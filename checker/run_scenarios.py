"""Multi-run verification through the real command line.

    python3 -m checker.run_scenarios [--seed 7 | --requests R --faults F] [--crash-samples 6]

It runs the scenario, checks it, and compares byte for byte against a fresh
run, a replay, and runs interrupted by CRASH_AFTER at sampled write counts
(plus both boundaries and the window right after a cloud commit). The agent
report is only read after `agent` exits 0, and a stale report is removed
first so it can never be mistaken for a new one.

Exit codes: 0 everything holds, 1 a check failed.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

from checker import check

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRASH_EXIT_CODE = 3
REPORT = "agent_report.jsonl"


class Failure(Exception):
    pass


def _run(args, crash_after=None):
    env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
    if crash_after is not None:
        env["CRASH_AFTER"] = str(crash_after)
    return subprocess.run([sys.executable, "-m", *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


class State:
    def __init__(self, path, requests, faults):
        self.path, self.requests, self.faults = path, requests, faults

    def cli(self, *args, crash_after=None):
        return _run(["tenant_lifecycle", "--state-dir", self.path, *args], crash_after)

    def provision(self, crash_after=None, expect=0):
        return self._expect(self.cli("provision", self.requests, self.faults,
                                     crash_after=crash_after), expect, "provision")

    def agent(self, crash_after=None, expect=0):
        report = os.path.join(self.path, REPORT)
        if os.path.exists(report):
            os.remove(report)  # never read a report this run did not write
        return self._expect(self.cli("agent", crash_after=crash_after), expect, "agent")

    def dump(self):
        return self._expect(self.cli("dump"), 0, "dump").stdout

    def report(self):
        with open(os.path.join(self.path, REPORT)) as f:
            return f.read()

    def _expect(self, result, code, what):
        if result.returncode != code:
            raise Failure(f"{what} in {self.path} exited {result.returncode}, expected {code}: "
                          f"{result.stderr.strip()[-300:]}")
        return result


def _rows(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _committed_writes(dump_text):
    # A write is one committed transaction. Each registry commit adds one audit
    # row and each resource commit one cloud_log row; storing the fault plan is
    # one more. The boundary check below confirms this count with real crashes.
    rows = _rows(dump_text)
    return sum(1 for r in rows if r["table"] in ("audit", "cloud_log")) + 1


def _in_cloud_window(dump_text):
    """True if some create is unresolved in the registry but already in the inventory."""
    rows = _rows(dump_text)
    present = {r["name"] for r in rows if r["table"] == "inventory"}
    return any(r["table"] == "step_attempts" and r["outcome"] is None
               and f"{r['env_id']}-{r['step']}" in present for r in rows)


def verify(requests, faults, workdir, crash_samples, seed, time_limit):
    results = {"checks": [], "crash_points": []}

    def note(name, ok, detail=""):
        results["checks"].append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            raise Failure(f"{name}: {detail}")

    def new_state(name):
        return State(os.path.join(workdir, name), requests, faults)

    def independent_check(state, dump_text):
        violations = check.check(_rows(dump_text), _rows(state.report()),
                                 check.read_requests(requests), check.read_jsonl(faults))
        note(f"checker:{os.path.basename(state.path)}", not violations,
             json.dumps(violations[:5]))

    base = new_state("baseline")
    start = time.perf_counter()
    base.provision()
    base.agent()
    runtime = time.perf_counter() - start
    results["runtime_seconds"] = round(runtime, 2)
    note("runtime", runtime < time_limit, f"{runtime:.2f}s for provision + agent")
    base_dump, base_report = base.dump(), base.report()
    independent_check(base, base_dump)

    fresh = new_state("fresh")
    fresh.provision()
    fresh.agent()
    note("fresh_run_identical", fresh.dump() == base_dump and fresh.report() == base_report)

    base.provision()
    base.agent()
    note("replay_writes_nothing", base.dump() == base_dump and base.report() == base_report)

    only = new_state("provision-only")
    only.provision()
    provision_writes = _committed_writes(only.dump())
    agent_writes = _committed_writes(base_dump) - provision_writes

    # Boundaries: the last write must crash, one past it must not.
    probe = new_state("boundary-last")
    probe.provision(crash_after=provision_writes, expect=CRASH_EXIT_CODE)
    probe = new_state("boundary-past")
    probe.provision(crash_after=provision_writes + 1)
    note("provision_write_count_confirmed", True, f"{provision_writes} writes")

    def provision_crash(n, only_if_in_window=False):
        s = new_state(f"provision-crash-{n}")
        s.provision(crash_after=n, expect=CRASH_EXIT_CODE)
        in_window = _in_cloud_window(s.dump())
        if only_if_in_window and not in_window:
            shutil.rmtree(s.path)
            return False
        s.provision()
        s.agent()
        ok = s.dump() == base_dump and s.report() == base_report
        results["crash_points"].append({"command": "provision", "crash_after": n,
                                        "cloud_window": in_window, "identical": ok})
        note(f"provision_crash_{n}", ok)
        shutil.rmtree(s.path)
        return in_window

    rng = random.Random(seed)
    points = {1, provision_writes} | {rng.randint(1, provision_writes)
                                      for _ in range(crash_samples)}
    window_hit = False
    for n in sorted(points):
        window_hit = provision_crash(n) or window_hit
    # Make sure at least one crash lands between a cloud commit and its registry outcome.
    for n in range(provision_writes // 2, provision_writes // 2 + 15):
        if window_hit:
            break
        if n not in points:
            window_hit = provision_crash(n, only_if_in_window=True)
    note("cloud_commit_window_exercised", window_hit)

    # The agent, not a provision rerun, picks up a crash that left an attempt open.
    # Different commands run, so the result is judged by the checker, not by bytes.
    window_points = [p["crash_after"] for p in results["crash_points"] if p["cloud_window"]]
    s = new_state("agent-recovers-provision")
    s.provision(crash_after=window_points[0], expect=CRASH_EXIT_CODE)
    s.agent()
    s.provision()
    s.agent()
    recovered_dump = s.dump()
    states = {r["state"] for r in _rows(recovered_dump) if r["table"] == "environments"}
    note("agent_recovery_reaches_final_states", states <= {"active", "escalated", "cleaned_up"},
         str(sorted(states)))
    independent_check(s, recovered_dump)
    shutil.rmtree(s.path)

    probe = new_state("agent-boundary-past")
    shutil.copytree(only.path, probe.path)
    probe.agent(crash_after=agent_writes + 1)
    note("agent_write_count_confirmed", probe.dump() == base_dump, f"{agent_writes} writes")

    agent_points = {1, agent_writes} | {rng.randint(1, agent_writes)
                                        for _ in range(crash_samples)}
    for n in sorted(agent_points):
        s = new_state(f"agent-crash-{n}")
        shutil.copytree(only.path, s.path)
        s.agent(crash_after=n, expect=CRASH_EXIT_CODE)
        if n == min(agent_points - {1}, default=1):
            s.provision()  # provision in between must not touch agent-owned environments
        s.agent()
        ok = s.dump() == base_dump and s.report() == base_report
        results["crash_points"].append({"command": "agent", "crash_after": n, "identical": ok})
        note(f"agent_crash_{n}", ok)
        shutil.rmtree(s.path)
    results["provision_writes"], results["agent_writes"] = provision_writes, agent_writes
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="checker.run_scenarios")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--requests")
    parser.add_argument("--faults")
    parser.add_argument("--crash-samples", type=int, default=6)
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--workdir", help="keep run directories here instead of a temp dir")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = args.workdir or tmp
        os.makedirs(workdir, exist_ok=True)
        requests, faults = args.requests, args.faults
        if requests is None or faults is None:
            fixture_dir = os.path.join(workdir, "fixtures")
            result = _run(["fixtures.gen_fixtures", "--seed", str(args.seed), "--out",
                           fixture_dir])
            if result.returncode != 0:
                print(f"error: fixture generation failed: {result.stderr}", file=sys.stderr)
                return 1
            requests = os.path.join(fixture_dir, "requests.jsonl")
            faults = os.path.join(fixture_dir, "faults.jsonl")
        try:
            results = verify(os.path.abspath(requests), os.path.abspath(faults), workdir,
                             args.crash_samples, args.seed, args.time_limit)
        except Failure as e:
            print(json.dumps({"ok": False, "failure": str(e)}, sort_keys=True))
            return 1
    print(json.dumps(dict(results, ok=True), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
