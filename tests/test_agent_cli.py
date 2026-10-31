"""End-to-end tests of `python -m tenant_lifecycle agent` in subprocesses."""

import json
import os
import shutil
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
    {"request_id": "r3", "tenant": "initech", "tier": "basic", "region": "us-east-1"},
    {"request_id": "r4", "tenant": "hooli", "tier": "enterprise", "region": "eu-west-1"},
]


def fault(request_id, step, kind, attempt=1):
    return {"request_id": request_id, "step": step, "op": "create", "attempt": attempt,
            "kind": kind}


FAULTS = [
    fault("r1", "secret", "timeout"),
    fault("r2", "database", "quota"),
    fault("r3", "dns", "timeout"),
    fault("r3", "dns", "duplicate", attempt=2),
    fault("r3", "dns", "timeout", attempt=3),
    fault("r4", "namespace", "duplicate"),
    fault("r4", "app", "timeout"),
    fault("r4", "app", "timeout_after_success", attempt=2),
]
TABLES = (("registry.db", ("environments", "step_attempts", "resources", "audit",
                           "policy_decisions")),
          ("inventory.db", ("inventory", "cloud_log", "fault_plan")))


def write_jsonl(path, records):
    with open(path, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def committed_writes(snap):
    return len(snap["audit"]) + len(snap["cloud_log"]) + len(snap["fault_plan"])


class AgentCliTestCase(unittest.TestCase):
    FAULT_RECORDS = FAULTS

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = self.path("requests.jsonl")
        self.faults = self.path("faults.jsonl")
        write_jsonl(self.requests, REQUESTS)
        write_jsonl(self.faults, self.FAULT_RECORDS)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def cli(self, state_dir, *args, crash_after=None):
        env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
        if crash_after is not None:
            env["CRASH_AFTER"] = str(crash_after)
        return subprocess.run(
            [sys.executable, "-m", "tenant_lifecycle", "--state-dir", state_dir, *args],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)

    def provision(self, state_dir, faults=None):
        return self.cli(state_dir, "provision", self.requests, faults or self.faults)

    def agent(self, state_dir, crash_after=None):
        return self.cli(state_dir, "agent", crash_after=crash_after)

    def snapshot(self, state_dir):
        out = {}
        for db, tables in TABLES:
            conn = sqlite3.connect(os.path.join(state_dir, db))
            for t in tables:
                out[t] = conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            conn.close()
        return out

    def report(self, state_dir):
        with open(os.path.join(state_dir, "agent_report.jsonl")) as f:
            return f.read()

    def provisioned(self, name):
        state = self.path(name)
        result = self.provision(state)
        self.assertEqual(result.returncode, 0, result.stderr)
        return state


class AgentCommandTest(AgentCliTestCase):
    def test_agent_retries_escalates_and_reports_decisions(self):
        state = self.provisioned("a")
        result = self.agent(state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.report(state))
        self.assertEqual(json.loads(result.stderr), {"active": 2, "escalated": 2})
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        for r in rows:
            self.assertTrue({"action", "evidence", "executed", "outcome"} <= set(r))
        self.assertEqual([(r["request_id"], r["action"], r["target"], r["rule"]) for r in rows],
                         [("r1", "retry_step", "secret/2", "retry_allowed"),
                          ("r1", "resume", "secret/2", "resume_allowed"),
                          ("r2", "retry_step", "database/2", "quota_not_retryable"),
                          ("r2", "escalate", "database/create/1", "escalate_allowed"),
                          ("r3", "retry_step", "dns/2", "retry_allowed"),
                          ("r3", "retry_step", "dns/3", "retry_allowed"),
                          ("r3", "retry_step", "dns/4", "retry_budget_exhausted"),
                          ("r3", "escalate", "dns/create/3", "escalate_allowed"),
                          ("r4", "retry_step", "namespace/2", "retry_allowed"),
                          ("r4", "resume", "namespace/2", "resume_allowed"),
                          ("r4", "retry_step", "app/2", "retry_allowed"),
                          ("r4", "resume", "app/2", "resume_allowed")])

    def test_full_pipeline_is_deterministic_and_replay_writes_nothing(self):
        for name in ("a", "b"):
            self.provisioned(name)
            self.assertEqual(self.agent(self.path(name)).returncode, 0)
        first, report = self.snapshot(self.path("a")), self.report(self.path("a"))
        self.assertEqual(self.snapshot(self.path("b")), first)
        self.assertEqual(self.report(self.path("b")), report)

        self.assertEqual(self.provision(self.path("a")).returncode, 0)
        self.assertEqual(self.agent(self.path("a")).returncode, 0)
        self.assertEqual(self.snapshot(self.path("a")), first)
        self.assertEqual(self.report(self.path("a")), report)

    def test_changed_faults_are_rejected_without_writes(self):
        state = self.provisioned("a")
        before = self.snapshot(state)
        other = self.path("other_faults.jsonl")
        write_jsonl(other, FAULTS[1:])
        result = self.provision(state, faults=other)
        self.assertEqual(result.returncode, 2)
        self.assertIn("faults differ", result.stderr)
        self.assertEqual(self.snapshot(state), before)

    def test_agent_needs_a_provisioned_state(self):
        result = self.agent(self.path("empty"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("run provision first", result.stderr)


def delete_fault(request_id, step, kind, attempt=1):
    return dict(fault(request_id, step, kind, attempt), op="delete")


# Escalation with failed deletes: r2 recovers within the cleanup budget,
# r4 exhausts it and must stay cleanup_failed with its resource reported.
COMPENSATION_FAULTS = [
    fault("r1", "secret", "timeout"),
    fault("r2", "dns", "quota"),
    delete_fault("r2", "secret", "timeout"),
    delete_fault("r2", "secret", "duplicate", attempt=2),
    fault("r3", "app", "timeout"),
    fault("r3", "app", "timeout", attempt=2),
    fault("r3", "app", "timeout", attempt=3),
    delete_fault("r3", "database", "timeout_after_success"),
    fault("r4", "database", "quota"),
] + [delete_fault("r4", "namespace", "timeout", attempt=n) for n in (1, 2, 3)]


class AgentCrashRecoveryTest(AgentCliTestCase):
    def test_crash_after_every_agent_write_then_rerun_matches_uninterrupted_run(self):
        template = self.provisioned("template")
        before_agent = committed_writes(self.snapshot(template))
        baseline_dir = self.path("baseline")
        shutil.copytree(template, baseline_dir)
        self.assertEqual(self.agent(baseline_dir).returncode, 0)
        baseline, baseline_report = self.snapshot(baseline_dir), self.report(baseline_dir)
        agent_writes = committed_writes(baseline) - before_agent
        self.assertGreater(agent_writes, 0)

        for n in range(1, agent_writes + 1):
            with self.subTest(crash_after=n):
                state = self.path(f"crash-{n}")
                shutil.copytree(template, state)
                crashed = self.agent(state, crash_after=n)
                self.assertEqual(crashed.returncode, writes.CRASH_EXIT_CODE, crashed.stderr)
                self.assertEqual(committed_writes(self.snapshot(state)) - before_agent, n)

                resumed = self.agent(state)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(self.snapshot(state), baseline)
                self.assertEqual(self.report(state), baseline_report)
                shutil.rmtree(state)

    def test_provision_between_agent_crash_and_rerun_changes_nothing(self):
        template = self.provisioned("template")
        baseline_dir = self.path("baseline")
        shutil.copytree(template, baseline_dir)
        self.assertEqual(self.agent(baseline_dir).returncode, 0)
        baseline = self.snapshot(baseline_dir)
        agent_writes = committed_writes(baseline) - committed_writes(self.snapshot(template))

        for n in range(1, agent_writes + 1):
            with self.subTest(crash_after=n):
                state = self.path(f"crash-{n}")
                shutil.copytree(template, state)
                self.assertEqual(self.agent(state, crash_after=n).returncode,
                                 writes.CRASH_EXIT_CODE)
                crashed = self.snapshot(state)
                result = self.provision(state)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.snapshot(state), crashed)
                self.assertEqual(self.agent(state).returncode, 0)
                self.assertEqual(self.snapshot(state), baseline)
                shutil.rmtree(state)


class CompensationCrashRecoveryTest(AgentCrashRecoveryTest):
    """The same crash sweeps, over escalation, failed deletes and cleanup_orphan."""
    FAULT_RECORDS = COMPENSATION_FAULTS

    def test_final_states_and_no_false_cleanup(self):
        state = self.provisioned("final")
        self.assertEqual(self.agent(state).returncode, 0)
        conn = sqlite3.connect(os.path.join(state, "registry.db"))
        states = dict(conn.execute("SELECT request_id, state FROM environments").fetchall())
        conn.close()
        self.assertEqual(states, {"r1": "active", "r2": "escalated", "r3": "escalated",
                                  "r4": "cleanup_failed"})
        conn = sqlite3.connect(os.path.join(state, "inventory.db"))
        owners = {r[0] for r in conn.execute("SELECT owner_env_id FROM inventory")}
        conn.close()
        rows = [json.loads(line) for line in self.report(state).splitlines()]
        escalated_envs = {r["env_id"] for r in rows
                          if r["outcome"] and r["outcome"]["env_state"] == "escalated"}
        self.assertTrue(escalated_envs)
        self.assertFalse(escalated_envs & owners)


if __name__ == "__main__":
    unittest.main()
