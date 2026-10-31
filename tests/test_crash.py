"""Crash safety across the registry and inventory files.

The scenario below follows the agreed protocol by hand: intent, then check
the inventory before any call, then record what the inventory shows. It is
test scaffolding, not the workflow.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tenant_lifecycle import writes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCENARIO = r"""
import sys
from tenant_lifecycle import models
from tenant_lifecycle.cloud import Cloud, CloudError, parse_faults
from tenant_lifecycle.registry import Registry

reg = Registry(sys.argv[1])
cloud = Cloud(sys.argv[2], parse_faults([
    {"request_id": "req-1", "step": "namespace", "op": "create", "attempt": 1,
     "kind": "timeout_after_success"},
    {"request_id": "req-1", "step": "database", "op": "create", "attempt": 1, "kind": "quota"},
]))
payload = {"request_id": "req-1", "tenant": "acme", "tier": "basic", "region": "eu-west-1"}
env_id, _ = reg.register_request("req-1", 1, "acme", "basic", "eu-west-1", payload)
env = reg.get_environment(env_id)
if env["state"] == models.PENDING:
    reg.transition(env_id, models.PROVISIONING, "workflow")

if reg.get_environment(env_id)["state"] == models.PROVISIONING:
    for step in ("namespace", "database"):
        reg.record_intent(env_id, step, "create", 1, "workflow")
        if not reg.open_attempts(env_id):
            continue
        name = models.resource_name(env_id, step)
        kind = models.resource_kind(step, "basic")
        parent = models.resource_parent(step, "basic", "eu-west-1")
        error = None
        if cloud.observe(name, env_id, step, kind, parent) == models.ABSENT:
            try:
                cloud.create(name, kind, env_id, step, parent, 1)
            except CloudError as e:
                error = e.error_class
        outcome = cloud.observe(name, env_id, step, kind, parent)
        reg.record_outcome(env_id, step, "create", 1, outcome,
                           error if outcome == models.ABSENT else None, "workflow")
    reg.transition(env_id, models.STEP_FAILED, "workflow", reason="quota")
"""

TOTAL_WRITES = 8  # register, provisioning, 2 intents, 1 cloud create, 2 outcomes, step_failed


def run(tmp, crash_after=None):
    env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
    if crash_after is not None:
        env["CRASH_AFTER"] = str(crash_after)
    return subprocess.run(
        [sys.executable, "-c", SCENARIO, os.path.join(tmp, "registry.db"),
         os.path.join(tmp, "inventory.db")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    ).returncode


def snapshot(tmp):
    out = {}
    for db, tables in (("registry.db", ("environments", "step_attempts", "resources", "audit")),
                       ("inventory.db", ("inventory", "cloud_log"))):
        conn = sqlite3.connect(os.path.join(tmp, db))
        for t in tables:
            out[t] = conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
        conn.close()
    return out


def committed_writes(snap):
    # Every registry transaction writes exactly one audit row and every cloud
    # transaction writes exactly one log row, so these count all commits.
    return len(snap["audit"]) + len(snap["cloud_log"])


class CrashTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        assert run(cls._tmp.name) == 0
        cls.baseline = snapshot(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_baseline_has_the_expected_writes_and_outcomes(self):
        self.assertEqual(committed_writes(self.baseline), TOTAL_WRITES)
        outcomes = [(r[1], r[5], r[6]) for r in self.baseline["step_attempts"]]
        self.assertEqual(sorted(outcomes),
                         [("database", "absent", "quota"), ("namespace", "present", None)])
        self.assertEqual([r[5] for r in self.baseline["cloud_log"]], ["timeout"])

    def test_crash_after_each_write_then_resume_matches_baseline(self):
        for n in range(1, TOTAL_WRITES + 1):
            with self.subTest(crash_after=n), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(run(tmp, n), writes.CRASH_EXIT_CODE)
                self.assertEqual(committed_writes(snapshot(tmp)), n)
                self.assertEqual(run(tmp), 0)
                self.assertEqual(snapshot(tmp), self.baseline)

    def test_crash_between_cloud_commit_and_registry_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(tmp, 4), writes.CRASH_EXIT_CODE)
            snap = snapshot(tmp)
            self.assertEqual(len(snap["inventory"]), 1)
            open_attempts = [r for r in snap["step_attempts"] if r[5] is None]
            self.assertEqual([(r[1], r[2], r[3]) for r in open_attempts],
                             [("namespace", "create", 1)])
            self.assertEqual(run(tmp), 0)
            self.assertEqual(snapshot(tmp), self.baseline)

    def test_limit_beyond_last_write_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(tmp, TOTAL_WRITES + 1), 0)
            self.assertEqual(snapshot(tmp), self.baseline)

    def test_replay_after_completion_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(tmp), 0)
            self.assertEqual(run(tmp), 0)
            self.assertEqual(snapshot(tmp), self.baseline)


class CrashLimitParsingTest(unittest.TestCase):
    def test_invalid_values_are_rejected(self):
        for raw in ("0", "-1", "abc", "1.5"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"CRASH_AFTER": raw}):
                with self.assertRaises(ValueError):
                    writes.crash_limit()

    def test_unset_or_empty_means_no_limit(self):
        with mock.patch.dict(os.environ, {"CRASH_AFTER": ""}):
            self.assertIsNone(writes.crash_limit())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(writes.crash_limit())


if __name__ == "__main__":
    unittest.main()
