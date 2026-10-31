import os
import sqlite3
import tempfile
import unittest

from tenant_lifecycle import agent, models, policy, workflow, writes
from tenant_lifecycle.cloud import Cloud, parse_faults
from tenant_lifecycle.registry import Registry


DIGEST = agent.load_rules()[1]


def request(request_id, tier="basic"):
    return {"request_id": request_id, "tenant": "acme", "tier": tier, "region": "eu-west-1"}


def fault(request_id, step, kind, attempt=1):
    return {"request_id": request_id, "step": step, "op": "create", "attempt": attempt,
            "kind": kind}


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Registry(os.path.join(self.tmp.name, "registry.db"))
        self.cloud = Cloud(os.path.join(self.tmp.name, "inventory.db"), {})

    def tearDown(self):
        self.reg.close()
        self.cloud.close()
        self.tmp.cleanup()

    def provision(self, requests, faults=()):
        self.cloud.faults = parse_faults(faults)
        workflow.provision(self.reg, self.cloud, list(enumerate(requests, start=1)))

    def failed(self, step, kind, request_id="r1"):
        self.provision([request(request_id)], [fault(request_id, step, kind)])
        return models.env_id_for(request_id)

    def check(self, env_id, action, target):
        allowed, rule, _ = policy.evaluate(self.reg, env_id, action, target)
        return allowed, rule

    def snapshot(self):
        return {t: self.reg.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("environments", "step_attempts", "audit", "policy_decisions")}


class RetryRuleTest(PolicyTestCase):
    def test_timeout_and_false_duplicate_are_retryable(self):
        for kind in ("timeout", "duplicate"):
            with self.subTest(kind=kind):
                env_id = self.failed("secret", kind, request_id=f"r-{kind}")
                self.assertEqual(self.check(env_id, "retry_step", "secret/2"),
                                 (True, "retry_allowed"))

    def test_quota_is_never_retried(self):
        env_id = self.failed("database", "quota")
        self.assertEqual(self.check(env_id, "retry_step", "database/2"),
                         (False, "quota_not_retryable"))

    def test_foreign_owner_and_mismatch_are_not_retryable(self):
        env_id = models.env_id_for("r1")
        name = models.resource_name(env_id, "dns")
        self.cloud.create(name, "dns", models.env_id_for("other"), "dns", None, 1)
        self.provision([request("r1")])
        self.assertEqual(self.check(env_id, "retry_step", "dns/2"),
                         (False, "outcome_not_retryable"))

    def test_budget_is_two_retries_after_the_initial_attempt(self):
        faults = [fault("r1", "app", "timeout", attempt=n) for n in (1, 2, 3)]
        self.provision([request("r1")], faults)
        env_id = models.env_id_for("r1")
        for attempt in (2, 3):
            decision = policy.decide(self.reg, env_id, "retry_step", f"app/{attempt}", DIGEST)
            self.assertTrue(decision["allowed"])
            workflow.execute(self.reg, self.cloud, decision["decision_id"])
        self.assertEqual(self.check(env_id, "retry_step", "app/4"),
                         (False, "retry_budget_exhausted"))
        self.assertEqual(policy.MAX_ATTEMPTS, 3)

    def test_only_the_failed_step_and_next_attempt_can_be_retried(self):
        env_id = self.failed("dns", "timeout")
        self.assertEqual(self.check(env_id, "retry_step", "secret/2"),
                         (False, "not_the_failed_step"))
        self.assertEqual(self.check(env_id, "retry_step", "dns/3"),
                         (False, "attempt_out_of_order"))
        self.assertEqual(self.check(env_id, "retry_step", "dns/x"),
                         (False, "attempt_out_of_order"))

    def test_active_environment_cannot_be_retried(self):
        self.provision([request("r1")])
        self.assertEqual(self.check(models.env_id_for("r1"), "retry_step", "app/2"),
                         (False, "env_not_step_failed"))


class ResumeRuleTest(PolicyTestCase):
    def test_resume_needs_provisioning_state(self):
        env_id = self.failed("dns", "timeout")
        self.assertEqual(self.check(env_id, "resume", "dns/1"), (False, "env_not_provisioning"))

    def test_resume_is_allowed_with_an_unresolved_create_and_records_it(self):
        req = request("r1")
        env_id, _ = self.reg.register_request("r1", 1, "acme", "basic", "eu-west-1", req)
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        allowed, rule, inputs = policy.evaluate(self.reg, env_id, "resume", "namespace/1")
        self.assertEqual((allowed, rule, inputs["open_attempts"]), (True, "resume_allowed", 1))

    def test_resume_is_allowed_for_a_pending_environment(self):
        req = request("r1")
        env_id, _ = self.reg.register_request("r1", 1, "acme", "basic", "eu-west-1", req)
        self.assertEqual(self.check(env_id, "resume", "start"), (True, "resume_allowed"))


class AllowlistTest(PolicyTestCase):
    def test_actions_outside_the_allowlist_are_denied(self):
        env_id = self.failed("dns", "timeout")
        for action in ("delete_environment", "create_resource", ""):
            with self.subTest(action=action):
                self.assertEqual(self.check(env_id, action, "dns/2"),
                                 (False, "action_not_allowed"))

    def test_cleanup_and_escalate_are_judged_by_their_own_rules(self):
        env_id = self.failed("dns", "timeout")
        self.assertEqual(self.check(env_id, "cleanup_orphan", "dns/2"),
                         (False, "env_not_cleanup_failed"))
        self.assertEqual(self.check(env_id, "escalate", "dns/create/1"),
                         (False, "retry_still_possible"))


class DurableDecisionTest(PolicyTestCase):
    def test_evaluate_writes_nothing(self):
        env_id = self.failed("dns", "timeout")
        before, start = self.snapshot(), writes.committed_count()
        policy.evaluate(self.reg, env_id, "retry_step", "dns/2")
        self.assertEqual((self.snapshot(), writes.committed_count()), (before, start))

    def test_decision_is_stored_with_its_inputs_and_audited_once(self):
        env_id = self.failed("database", "quota")
        decision = policy.decide(self.reg, env_id, "retry_step", "database/2", DIGEST)
        self.assertEqual((decision["allowed"], decision["rule"]), (0, "quota_not_retryable"))
        self.assertIn('"error_class": "quota"', decision["inputs"])

        before, start = self.snapshot(), writes.committed_count()
        again = policy.decide(self.reg, env_id, "retry_step", "database/2", DIGEST)
        self.assertEqual(tuple(again), tuple(decision))
        self.assertEqual((self.snapshot(), writes.committed_count()), (before, start))
        audited = [e for e in self.reg.audit_events(env_id) if e["kind"] == "policy_decision"]
        self.assertEqual([e["ref"] for e in audited], [decision["decision_id"]])

    def test_decisions_are_immutable(self):
        env_id = self.failed("dns", "timeout")
        decision = policy.decide(self.reg, env_id, "retry_step", "dns/2", DIGEST)
        with self.assertRaises(sqlite3.IntegrityError):
            self.reg.conn.execute("UPDATE policy_decisions SET allowed = 0")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reg.conn.execute("DELETE FROM policy_decisions WHERE decision_id = ?",
                                  (decision["decision_id"],))

    def test_executor_refuses_denied_or_unknown_decisions(self):
        env_id = self.failed("database", "quota")
        denied = policy.decide(self.reg, env_id, "retry_step", "database/2", DIGEST)
        before = self.snapshot()
        for decision_id in (denied["decision_id"], f"{env_id}:retry_step:database/9"):
            with self.subTest(decision_id=decision_id), self.assertRaises(PermissionError):
                workflow.execute(self.reg, self.cloud, decision_id)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
