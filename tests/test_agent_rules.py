"""agent_rules.json decides what the agent proposes; the policy still decides what is allowed."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from checker import check
from tenant_lifecycle import agent, models

from test_agent import AgentTestCase, fault, request
from test_checker import write_jsonl

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RULES, DEFAULT_DIGEST = agent.load_rules()


def write_rules(path, rules):
    with open(path, "w") as f:
        json.dump(rules, f)
    return path


class RulesControlProposalsTest(AgentTestCase):
    def run_with(self, rules):
        path = write_rules(os.path.join(self.tmp.name, "rules.json"), rules)
        loaded, digest = agent.load_rules(path)

        def execute(decision_id):
            from tenant_lifecycle import workflow
            workflow.execute(self.reg, self.cloud, decision_id)

        agent.run(self.reg, execute, loaded, digest)
        return digest

    def test_empty_rules_propose_nothing(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_with({})
        self.assertEqual(self.reg.decisions(), [])
        self.assertEqual(self.state("r1"), models.STEP_FAILED)

    def test_order_in_the_file_is_the_order_of_proposals(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_with(dict(DEFAULT_RULES, step_failed=["escalate", "retry_step"]))
        # escalate is tried first and denied by the policy; retry_step is then approved.
        self.assertEqual(self.decisions("r1")[:2],
                         [("escalate", "dns/create/1", False, "retry_still_possible"),
                          ("retry_step", "dns/2", True, "retry_allowed")])
        self.assertEqual(self.state("r1"), models.ACTIVE)

    def test_an_action_left_out_is_never_proposed(self):
        self.provision([request("r1", tier="enterprise")], [fault("r1", "database", "quota")])
        self.run_with(dict(DEFAULT_RULES, step_failed=["retry_step"]))
        self.assertEqual(self.decisions("r1"),
                         [("retry_step", "database/2", False, "quota_not_retryable")])
        self.assertEqual(self.state("r1"), models.STEP_FAILED)

    def test_a_state_left_out_is_not_recovered(self):
        req = request("r1")
        env_id, _ = self.reg.register_request("r1", 1, "acme", "basic", "eu-west-1", req)
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.run_with({k: v for k, v in DEFAULT_RULES.items() if k != "provisioning"})
        self.assertEqual(self.reg.decisions(), [])
        self.assertEqual(self.state("r1"), models.PROVISIONING)

    def test_policy_still_denies_what_the_file_proposes_wrongly(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_with({"step_failed": ["escalate", "resume", "cleanup_orphan"]})
        self.assertEqual([(a, ok, rule) for a, _, ok, rule in self.decisions("r1")],
                         [("escalate", False, "retry_still_possible"),
                          ("resume", False, "env_not_provisioning")])
        self.assertEqual(self.state("r1"), models.STEP_FAILED)
        self.assertEqual([a for a in self.reg.attempts(models.env_id_for("r1"))
                          if a["actor"] == "agent"], [])

    def test_decisions_record_the_digest_of_the_rules_used(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        digest = self.run_with(DEFAULT_RULES)
        self.assertEqual(digest, DEFAULT_DIGEST)
        self.assertEqual({json.loads(d["inputs"])["agent_rules_sha256"]
                          for d in self.reg.decisions()}, {DEFAULT_DIGEST})

    def test_resuming_with_other_rules_is_refused_before_any_write(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_with({"step_failed": ["retry_step"]})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.run_with(DEFAULT_RULES)
        self.assertEqual(self.snapshot(), before)


class RulesValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rules.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_files_are_rejected(self):
        bad = {
            "not json": "{nope",
            "not an object": "[]",
            "unknown state": json.dumps({"active": ["resume"]}),
            "actions not a list": json.dumps({"step_failed": "retry_step"}),
            "repeated action": json.dumps({"step_failed": ["retry_step", "retry_step"]}),
            "unknown action": json.dumps({"step_failed": ["delete_tenant"]}),
            "non-string action": json.dumps({"step_failed": [{"action": "retry_step"}]}),
        }
        for label, text in bad.items():
            with self.subTest(label):
                with open(self.path, "w") as f:
                    f.write(text)
                with self.assertRaises(ValueError):
                    agent.load_rules(self.path)

    def test_digest_ignores_formatting_but_not_content(self):
        with open(self.path, "w") as f:
            f.write(json.dumps(dict(reversed(list(DEFAULT_RULES.items()))), indent=4))
        self.assertEqual(agent.load_rules(self.path)[1], DEFAULT_DIGEST)
        write_rules(self.path, dict(DEFAULT_RULES, step_failed=["escalate", "retry_step"]))
        self.assertNotEqual(agent.load_rules(self.path)[1], DEFAULT_DIGEST)

    def test_shipped_rules_file_is_the_documented_default(self):
        self.assertEqual(DEFAULT_RULES, {"pending": ["resume"], "provisioning": ["resume"],
                                         "step_failed": ["retry_step", "escalate"],
                                         "cleanup_failed": ["cleanup_orphan", "escalate"]})


class RulesCliTest(unittest.TestCase):
    """The real CLI, run from a copy of the package so its rules file can be edited safely."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "pkg")
        shutil.copytree(os.path.join(REPO_ROOT, "tenant_lifecycle"),
                        os.path.join(self.root, "tenant_lifecycle"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.rules = os.path.join(self.root, "tenant_lifecycle", "agent_rules.json")
        self.requests = os.path.join(self.tmp.name, "r.jsonl")
        self.faults = os.path.join(self.tmp.name, "f.jsonl")
        write_jsonl(self.requests, [request("r1"), request("r2", tier="enterprise")])
        write_jsonl(self.faults, [fault("r1", "dns", "timeout"), fault("r2", "secret", "quota")])
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, crash_after=None, state=None):
        env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
        if crash_after is not None:
            env["CRASH_AFTER"] = str(crash_after)
        return subprocess.run([sys.executable, "-m", "tenant_lifecycle", "--state-dir",
                               state or self.state, *args], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)

    def state_digest(self):
        h = hashlib.sha256()
        for name in ("registry.db", "inventory.db"):
            with open(os.path.join(self.state, name), "rb") as f:
                h.update(f.read())
        return h.hexdigest()

    def test_invalid_rules_exit_2_without_writing(self):
        self.assertEqual(self.cli("provision", self.requests, self.faults).returncode, 0)
        before = self.state_digest()
        write_rules(self.rules, {"step_failed": ["delete_tenant"]})
        result = self.cli("agent")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("unknown actions", result.stderr)
        self.assertEqual(self.state_digest(), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "agent_report.jsonl")))

    def test_rules_changed_between_crash_and_resume_are_rejected(self):
        baseline = os.path.join(self.tmp.name, "baseline")
        self.assertEqual(self.cli("provision", self.requests, self.faults).returncode, 0)
        shutil.copytree(self.state, baseline)
        self.assertEqual(self.cli("agent", crash_after=2).returncode, 3)
        crashed = self.state_digest()

        with open(self.rules) as f:
            original = f.read()
        write_rules(self.rules, dict(DEFAULT_RULES, step_failed=["escalate", "retry_step"]))
        result = self.cli("agent")
        self.assertEqual(result.returncode, 2)
        self.assertIn("changed", result.stderr)
        self.assertEqual(self.state_digest(), crashed)

        # With the original rules back, the resume matches an uninterrupted agent run.
        with open(self.rules, "w") as f:
            f.write(original)
        self.assertEqual(self.cli("agent").returncode, 0)
        self.assertEqual(self.cli("agent", state=baseline).returncode, 0)
        self.assertEqual(self.cli("dump").stdout, self.cli("dump", state=baseline).stdout)

    def test_rules_file_changes_what_the_cli_agent_proposes(self):
        self.assertEqual(self.cli("provision", self.requests, self.faults).returncode, 0)
        write_rules(self.rules, {"step_failed": ["retry_step"]})
        result = self.cli("agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = [(r["request_id"], r["action"], r["allowed"])
                   for r in map(json.loads, result.stdout.splitlines())]
        self.assertEqual(actions, [("r1", "retry_step", True), ("r2", "retry_step", False)])
        # No "provisioning" rule, so r1 is never resumed; no "escalate", so r2 stays failed.
        self.assertEqual(json.loads(result.stderr), {"provisioning": 1, "step_failed": 1})


class DigestCheckerTest(AgentTestCase):
    def dump_and_report(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_agent()
        rows = []
        for d in self.reg.decisions():
            rows.append({"table": "policy_decisions", "decision_id": d["decision_id"],
                         "inputs": json.loads(d["inputs"])})
        return rows

    def test_mixed_or_missing_digests_are_flagged(self):
        rows = self.dump_and_report()
        self.assertTrue(len(rows) >= 2)
        data = {"decisions": {r["decision_id"]: r for r in rows}, "audit": {}, "envs": {}}
        findings = check.Findings()
        check._check_approvals(findings, data)
        self.assertFalse([v for v in findings.items if v["check"] == "P6_agent_rules_inconsistent"])

        for corrupt in ("other-digest", None):
            with self.subTest(corrupt=corrupt):
                changed = copy.deepcopy(data)
                first = next(iter(changed["decisions"].values()))
                if corrupt is None:
                    first["inputs"].pop("agent_rules_sha256")
                else:
                    first["inputs"]["agent_rules_sha256"] = corrupt
                findings = check.Findings()
                check._check_approvals(findings, changed)
                self.assertIn("P6_agent_rules_inconsistent",
                              {v["check"] for v in findings.items})


if __name__ == "__main__":
    unittest.main()
