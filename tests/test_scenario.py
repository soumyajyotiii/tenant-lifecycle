"""The full generated scenario through the real CLI: gen_fixtures, provision, agent, dump."""

import collections
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from fixtures import gen_fixtures
from tenant_lifecycle import writes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = 7
TIME_LIMIT_SECONDS = 60

# What each fault must look like in step_attempts once it has been exercised.
EXPECTED_OUTCOME = {
    ("create", "timeout"): ("absent", "timeout"),
    ("create", "duplicate"): ("absent", "already_exists"),
    ("create", "quota"): ("absent", "quota"),
    ("create", "timeout_after_success"): ("present", None),
    ("delete", "timeout"): ("present", "timeout"),
    ("delete", "duplicate"): ("present", "not_found"),
    ("delete", "timeout_after_success"): ("absent", None),
}


def run(*args, crash_after=None):
    env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
    if crash_after is not None:
        env["CRASH_AFTER"] = str(crash_after)
    return subprocess.run([sys.executable, "-m", *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


class Scenario:
    """One state directory driven only through the command line."""

    def __init__(self, root, name, fixture_dir):
        self.state = os.path.join(root, name)
        self.requests = os.path.join(fixture_dir, "requests.jsonl")
        self.faults = os.path.join(fixture_dir, "faults.jsonl")

    def cli(self, *args, crash_after=None):
        return run("tenant_lifecycle", "--state-dir", self.state, *args, crash_after=crash_after)

    def provision(self, crash_after=None):
        return self.cli("provision", self.requests, self.faults, crash_after=crash_after)

    def agent(self, crash_after=None):
        return self.cli("agent", crash_after=crash_after)

    def dump(self):
        result = self.cli("dump")
        assert result.returncode == 0, result.stderr
        return result.stdout

    def report(self):
        with open(os.path.join(self.state, "agent_report.jsonl")) as f:
            return f.read()

    def committed_writes(self):
        rows = [json.loads(line) for line in self.dump().splitlines()]
        tables = collections.Counter(r["table"] for r in rows)
        # One audit row per registry commit, one log row per resource commit, plus the fault plan.
        return tables["audit"] + tables["cloud_log"] + 1


class ScenarioTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = cls._tmp.name
        cls.fixture_dir = os.path.join(cls.root, "fixtures")
        result = run("fixtures.gen_fixtures", "--seed", str(SEED), "--out", cls.fixture_dir)
        assert result.returncode == 0, result.stderr

        cls.baseline = Scenario(cls.root, "baseline", cls.fixture_dir)
        start = time.perf_counter()
        cls.provision_result = cls.baseline.provision()
        cls.agent_result = cls.baseline.agent()
        cls.elapsed = time.perf_counter() - start
        cls.dump_text = cls.baseline.dump()
        cls.rows = [json.loads(line) for line in cls.dump_text.splitlines()]
        cls.tables = collections.defaultdict(list)
        for row in cls.rows:
            cls.tables[row["table"]].append(row)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def scenario(self, name):
        return Scenario(self.root, name, self.fixture_dir)


class FullScenarioTest(ScenarioTestCase):
    def test_commands_succeed_within_the_time_limit(self):
        self.assertEqual(self.provision_result.returncode, 0, self.provision_result.stderr)
        self.assertEqual(self.agent_result.returncode, 0, self.agent_result.stderr)
        self.assertLess(self.elapsed, TIME_LIMIT_SECONDS)

    def test_every_fault_is_exercised_with_the_expected_outcome(self):
        env_of = {e["request_id"]: e["env_id"] for e in self.tables["environments"]}
        attempts = {(a["env_id"], a["step"], a["op"], a["attempt"]): a
                    for a in self.tables["step_attempts"]}
        faults = read_jsonl(self.baseline.faults)
        self.assertTrue(faults)
        for f in faults:
            with self.subTest(fault=f):
                a = attempts.get((env_of[f["request_id"]], f["step"], f["op"], f["attempt"]))
                self.assertIsNotNone(a, "fault never reached")
                self.assertEqual((a["outcome"], a["error_class"]),
                                 EXPECTED_OUTCOME[(f["op"], f["kind"])])
        timeouts_logged = sum(1 for c in self.tables["cloud_log"] if c["response"] == "timeout")
        self.assertEqual(timeouts_logged,
                         sum(1 for f in faults if f["kind"] == "timeout_after_success"))

    def test_every_environment_reaches_a_valid_final_state(self):
        owned = collections.Counter(i["owner_env_id"] for i in self.tables["inventory"])
        envs = self.tables["environments"]
        self.assertEqual(len(envs), len({r["request_id"]
                                         for r in read_jsonl(self.baseline.requests)}))
        for e in envs:
            with self.subTest(request_id=e["request_id"]):
                if e["state"] == "active":
                    self.assertEqual(owned[e["env_id"]], len(gen_fixtures.STEPS))
                else:
                    self.assertIn(e["state"], ("escalated", "cleaned_up"))
                    self.assertEqual(owned[e["env_id"]], 0)
        self.assertEqual([a for a in self.tables["step_attempts"] if a["outcome"] is None], [])
        states = collections.Counter(e["state"] for e in envs)
        self.assertGreater(states["active"], 0)
        self.assertGreater(states["escalated"], 0)

    def test_every_cloud_resource_is_accounted_for_and_matches(self):
        resources = {r["name"]: r for r in self.tables["resources"]}
        for item in self.tables["inventory"]:
            with self.subTest(name=item["name"]):
                account = resources.get(item["name"])
                self.assertIsNotNone(account)
                self.assertEqual((account["env_id"], account["step"], account["kind"],
                                  account["parent"], account["status"]),
                                 (item["owner_env_id"], item["owner_step"], item["kind"],
                                  item["parent"], "present"))

    def test_duplicates_never_create_a_second_environment(self):
        lines = read_jsonl(self.baseline.requests)
        counts = collections.Counter(r["request_id"] for r in lines)
        duplicated = {rid for rid, n in counts.items() if n > 1}
        audited = {a["details"]["request_id"] for a in self.tables["audit"]
                   if a["kind"] == "duplicate_ignored"}
        self.assertEqual(audited, duplicated)
        self.assertEqual(sum(1 for a in self.tables["audit"] if a["kind"] == "duplicate_ignored"),
                         len(lines) - len(counts))


class DumpTest(ScenarioTestCase):
    TABLE_ORDER = ("environments", "step_attempts", "resources", "policy_decisions", "audit",
                   "inventory", "cloud_log")
    KEYS = {"environments": ("env_id",), "step_attempts": ("env_id", "step", "op", "attempt"),
            "resources": ("name",), "policy_decisions": ("decision_id",),
            "audit": ("env_id", "seq"), "inventory": ("name",), "cloud_log": ("seq",)}

    def test_dump_is_sorted_by_table_then_primary_key(self):
        def key(row):
            return (self.TABLE_ORDER.index(row["table"]),
                    tuple(row[k] for k in self.KEYS[row["table"]]))
        self.assertEqual([key(r) for r in self.rows], sorted(key(r) for r in self.rows))
        self.assertEqual(len({key(r) for r in self.rows}), len(self.rows))

    def test_each_line_is_canonical_json(self):
        for line in self.dump_text.splitlines()[:200]:
            self.assertEqual(line, json.dumps(json.loads(line), sort_keys=True,
                                              separators=(",", ":")))

    def test_dump_has_no_volatile_fields(self):
        columns = {k for r in self.rows for k in r}
        self.assertFalse({c for c in columns if "time" in c or c.endswith("_at")})

    def test_dump_does_not_change_the_state_files(self):
        def digest():
            h = hashlib.sha256()
            for name in ("registry.db", "inventory.db"):
                with open(os.path.join(self.baseline.state, name), "rb") as f:
                    h.update(f.read())
            return h.hexdigest()
        before = digest()
        self.assertEqual(self.baseline.dump(), self.dump_text)
        self.assertEqual(digest(), before)

    def test_dump_without_state_fails_and_creates_nothing(self):
        missing = self.scenario("missing")
        result = missing.cli("dump")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(missing.state))

    def test_dump_of_an_empty_state_dir_does_not_create_databases(self):
        empty = self.scenario("empty")
        os.makedirs(empty.state)
        self.assertEqual(empty.cli("dump").returncode, 2)
        self.assertEqual(os.listdir(empty.state), [])


class ReproducibilityTest(ScenarioTestCase):
    def test_fresh_run_gives_a_byte_identical_dump_and_report(self):
        again = self.scenario("again")
        self.assertEqual(again.provision().returncode, 0)
        self.assertEqual(again.agent().returncode, 0)
        self.assertEqual(again.dump(), self.dump_text)
        self.assertEqual(again.report(), self.baseline.report())

    def test_replaying_both_commands_changes_nothing(self):
        replay = self.scenario("replay")
        shutil.copytree(self.baseline.state, replay.state)
        report = replay.report()
        self.assertEqual(replay.provision().returncode, 0)
        self.assertEqual(replay.agent().returncode, 0)
        self.assertEqual(replay.dump(), self.dump_text)
        self.assertEqual(replay.report(), report)


class ScaleCrashRecoveryTest(ScenarioTestCase):
    """Sampled crash points at full scale; exhaustive sweeps run on small fixtures."""

    def provisioned_only(self):
        only = self.scenario("provision-only")
        if not os.path.exists(only.state):
            self.assertEqual(only.provision().returncode, 0)
        return only

    def test_provision_crash_points_recover_to_the_same_dump(self):
        total = self.provisioned_only().committed_writes()
        for n in sorted({1, 2, total // 3, total // 2 + 1, total - 1, total}):
            with self.subTest(crash_after=n):
                s = self.scenario(f"p-crash-{n}")
                self.assertEqual(s.provision(crash_after=n).returncode, writes.CRASH_EXIT_CODE)
                self.assertEqual(s.provision().returncode, 0)
                self.assertEqual(s.agent().returncode, 0)
                self.assertEqual(s.dump(), self.dump_text)

    def test_agent_crash_points_recover_to_the_same_dump(self):
        only = self.provisioned_only()
        before = only.committed_writes()
        agent_writes = self.baseline.committed_writes() - before
        for n in sorted({1, agent_writes // 3, agent_writes // 2 + 1, agent_writes - 1}):
            with self.subTest(crash_after=n):
                s = self.scenario(f"a-crash-{n}")
                shutil.copytree(only.state, s.state)
                self.assertEqual(s.agent(crash_after=n).returncode, writes.CRASH_EXIT_CODE)
                self.assertEqual(s.agent().returncode, 0)
                self.assertEqual(s.dump(), self.dump_text)
                self.assertEqual(s.report(), self.baseline.report())

    def test_crash_right_after_a_cloud_commit_recovers(self):
        """Find a crash point inside the cross-file window, then recover from it."""
        total = self.provisioned_only().committed_writes()
        for n in range(total // 2, total // 2 + 12):
            s = self.scenario(f"window-{n}")
            self.assertEqual(s.provision(crash_after=n).returncode, writes.CRASH_EXIT_CODE)
            rows = [json.loads(line) for line in s.dump().splitlines()]
            present = {r["name"] for r in rows if r["table"] == "inventory"}
            open_creates = [r for r in rows if r["table"] == "step_attempts"
                            and r["outcome"] is None and r["op"] == "create"
                            and f"{r['env_id']}-{r['step']}" in present]
            if open_creates:
                self.assertEqual(s.provision().returncode, 0)
                self.assertEqual(s.agent().returncode, 0)
                self.assertEqual(s.dump(), self.dump_text)
                return
            shutil.rmtree(s.state)
        self.fail("no crash point inside the cloud-commit window was found")


class FixtureGeneratorTest(unittest.TestCase):
    def generate(self, out, seed=SEED):
        result = run("fixtures.gen_fixtures", "--seed", str(seed), "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = {}
        for name in ("requests.jsonl", "faults.jsonl"):
            with open(os.path.join(out, name), "rb") as f:
                files[name] = f.read()
        return files

    def test_same_seed_gives_identical_files_and_other_seeds_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.generate(os.path.join(tmp, "a"))
            self.assertEqual(self.generate(os.path.join(tmp, "b")), first)
            self.assertNotEqual(self.generate(os.path.join(tmp, "c"), seed=SEED + 1), first)

    def test_required_coverage_holds_for_many_seeds(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                requests, faults = gen_fixtures.generate(seed)
                ids = collections.Counter(r["request_id"] for r in requests)
                creates = [f for f in faults if f["op"] == "create"]
                self.assertGreaterEqual(len(ids), 200)
                self.assertEqual({r["tier"] for r in requests}, {"basic", "enterprise"})
                self.assertGreaterEqual(sum(1 for n in ids.values() if n > 1), 10)
                self.assertGreaterEqual(len({f["request_id"] for f in creates}), 30)
                self.assertTrue(all(f["step"] != "namespace" for f in creates), "not partway")
                self.assertTrue(any(f["kind"] == "timeout_after_success" for f in creates))
                self.assertGreaterEqual(len({f["request_id"] for f in creates
                                             if f["kind"] == "quota"}), 5)
                self.assertGreaterEqual(len({f["request_id"] for f in faults
                                             if f["op"] == "delete"
                                             and f["kind"] != "timeout_after_success"}), 3)

    def test_faults_stay_within_the_budgets(self):
        for seed in range(20):
            requests, faults = gen_fixtures.generate(seed)
            per_step = collections.Counter((f["request_id"], f["step"], f["op"]) for f in faults)
            for (rid, step, op), n in per_step.items():
                with self.subTest(seed=seed, request_id=rid, step=step, op=op):
                    # Deletes must clear by attempt 3; creates may use all three attempts.
                    self.assertLessEqual(n, 2 if op == "delete" else 3)


if __name__ == "__main__":
    unittest.main()
