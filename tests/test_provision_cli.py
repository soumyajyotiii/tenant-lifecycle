"""End-to-end tests of `python -m tenant_lifecycle provision` in subprocesses."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tenant_lifecycle import writes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUESTS = [
    {"request_id": "r1", "tenant": "acme", "tier": "basic", "region": "eu-west-1"},
    {"request_id": "r2", "tenant": "globex", "tier": "enterprise", "region": "us-east-1"},
    {"request_id": "r1", "tenant": "acme", "tier": "basic", "region": "eu-west-1"},
    {"request_id": "r3", "tenant": "initech", "tier": "basic", "region": "us-east-1"},
]
FAULTS = [
    {"request_id": "r1", "step": "namespace", "op": "create", "attempt": 1,
     "kind": "timeout_after_success"},
    {"request_id": "r2", "step": "database", "op": "create", "attempt": 1, "kind": "quota"},
    {"request_id": "r3", "step": "dns", "op": "create", "attempt": 1, "kind": "duplicate"},
]


def write_jsonl(path, records):
    with open(path, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = os.path.join(self.tmp.name, "requests.jsonl")
        self.faults = os.path.join(self.tmp.name, "faults.jsonl")
        write_jsonl(self.requests, REQUESTS)
        write_jsonl(self.faults, FAULTS)

    def tearDown(self):
        self.tmp.cleanup()

    def provision(self, state_dir, crash_after=None, requests=None):
        env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
        if crash_after is not None:
            env["CRASH_AFTER"] = str(crash_after)
        return subprocess.run(
            [sys.executable, "-m", "tenant_lifecycle", "--state-dir", state_dir, "provision",
             requests or self.requests, self.faults],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)

    def state_dir(self, name):
        return os.path.join(self.tmp.name, name)

    def snapshot(self, state_dir):
        out = {}
        for db, tables in (("registry.db", ("environments", "step_attempts", "resources", "audit")),
                           ("inventory.db", ("inventory", "cloud_log", "fault_plan"))):
            conn = sqlite3.connect(os.path.join(state_dir, db))
            for t in tables:
                out[t] = conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            conn.close()
        return out


def committed_writes(snap):
    # Each registry commit writes one audit row; each cloud commit writes one log
    # row, except storing the fault plan, which is one more commit.
    return len(snap["audit"]) + len(snap["cloud_log"]) + len(snap["fault_plan"])


class ProvisionCommandTest(CliTestCase):
    def test_provision_reports_final_states(self):
        result = self.provision(self.state_dir("a"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"active": 1, "step_failed": 2})

    def test_fresh_runs_are_identical_and_rerun_writes_nothing(self):
        self.assertEqual(self.provision(self.state_dir("a")).returncode, 0)
        self.assertEqual(self.provision(self.state_dir("b")).returncode, 0)
        first = self.snapshot(self.state_dir("a"))
        self.assertEqual(self.snapshot(self.state_dir("b")), first)

        rerun = self.provision(self.state_dir("a"))
        self.assertEqual(rerun.returncode, 0)
        self.assertEqual(self.snapshot(self.state_dir("a")), first)

    def test_invalid_requests_file_writes_nothing(self):
        bad = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad, "w") as f:
            f.write(json.dumps(REQUESTS[0]) + "\n{broken\n")
        result = self.provision(self.state_dir("bad"), requests=bad)
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 2", result.stderr)
        self.assertFalse(os.path.exists(self.state_dir("bad")))

    def test_invalid_crash_after_writes_nothing(self):
        result = self.provision(self.state_dir("bad"), crash_after="soon")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(self.state_dir("bad")))

    def test_missing_faults_file_writes_nothing(self):
        os.remove(self.faults)
        result = self.provision(self.state_dir("bad"))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(self.state_dir("bad")))


class CrashRecoveryTest(CliTestCase):
    def test_crash_after_every_write_then_resume_matches_uninterrupted_run(self):
        self.assertEqual(self.provision(self.state_dir("baseline")).returncode, 0)
        baseline = self.snapshot(self.state_dir("baseline"))
        total = committed_writes(baseline)

        cloud_write_points = 0
        previous_log = 0
        for n in range(1, total + 1):
            with self.subTest(crash_after=n):
                state = self.state_dir(f"crash-{n}")
                crashed = self.provision(state, crash_after=n)
                self.assertEqual(crashed.returncode, writes.CRASH_EXIT_CODE, crashed.stderr)
                snap = self.snapshot(state)
                self.assertEqual(committed_writes(snap), n)
                if len(snap["cloud_log"]) > previous_log:
                    cloud_write_points += 1
                previous_log = len(snap["cloud_log"])

                resumed = self.provision(state)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(self.snapshot(state), baseline)

        # Every cloud commit was a crash point, so the window between the
        # inventory commit and the registry outcome was exercised for each one.
        self.assertEqual(cloud_write_points, len(baseline["cloud_log"]))

    def test_limit_beyond_last_write_completes_normally(self):
        self.assertEqual(self.provision(self.state_dir("baseline")).returncode, 0)
        baseline = self.snapshot(self.state_dir("baseline"))
        result = self.provision(self.state_dir("late"), crash_after=committed_writes(baseline) + 1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(self.state_dir("late")), baseline)


if __name__ == "__main__":
    unittest.main()
