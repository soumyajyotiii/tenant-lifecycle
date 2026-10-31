import json
import unittest
from unittest import mock

from tenant_lifecycle import agent, models, policy, workflow

from test_agent import DIGEST, AgentTestCase, fault, request


def delete_fault(request_id, step, kind, attempt=1):
    return dict(fault(request_id, step, kind, attempt), op="delete")


class CompensationTestCase(AgentTestCase):
    def run_agent_spying_deletes(self):
        with mock.patch.object(self.cloud, "delete", wraps=self.cloud.delete) as spy:
            self.run_agent()
        return [c.args[0] for c in spy.call_args_list]

    def owned(self, request_id):
        return self.cloud.owned_by(models.env_id_for(request_id))

    def name(self, request_id, step):
        return models.resource_name(models.env_id_for(request_id), step)

    def delete_attempts(self, request_id, step):
        return [(a["attempt"], a["outcome"], a["error_class"])
                for a in self.reg.attempts(models.env_id_for(request_id))
                if a["step"] == step and a["op"] == "delete"]


class EscalationTest(CompensationTestCase):
    def test_compensation_runs_in_reverse_order_then_escalates(self):
        self.provision([request("r1")], [fault("r1", "app", "quota")])
        deleted = self.run_agent_spying_deletes()
        self.assertEqual(deleted, [self.name("r1", s) for s in ("dns", "secret", "database",
                                                                "namespace")])
        self.assertEqual(self.state("r1"), models.ESCALATED)
        self.assertEqual(self.owned("r1"), [])

    def test_quota_on_first_step_escalates_with_nothing_to_delete(self):
        self.provision([request("r1")], [fault("r1", "namespace", "quota")])
        self.assertEqual(self.run_agent_spying_deletes(), [])
        self.assertEqual(self.state("r1"), models.ESCALATED)

    def test_retryable_failure_is_retried_before_any_escalation(self):
        self.provision([request("r1")], [fault("r1", "dns", "timeout")])
        self.run_agent()
        self.assertEqual(self.state("r1"), models.ACTIVE)
        self.assertNotIn("escalate", {d["action"] for d in self.reg.decisions()})

    def test_delete_timeout_after_success_is_accepted_after_inventory_check(self):
        self.provision([request("r1")], [fault("r1", "secret", "quota"),
                                         delete_fault("r1", "database", "timeout_after_success")])
        self.run_agent()
        self.assertEqual(self.delete_attempts("r1", "database"), [(1, models.ABSENT, None)])
        self.assertEqual(self.state("r1"), models.ESCALATED)


class FailedDeletionTest(CompensationTestCase):
    def test_failed_delete_stops_compensation_and_cleanup_orphan_retries_it(self):
        self.provision([request("r1")], [fault("r1", "app", "quota"),
                                         delete_fault("r1", "secret", "timeout")])
        self.run_agent()
        self.assertEqual(self.delete_attempts("r1", "secret"),
                         [(1, models.PRESENT, "timeout"), (2, models.ABSENT, None)])
        self.assertEqual([d[:3] for d in self.decisions("r1")],
                         [("retry_step", "app/2", False),
                          ("escalate", "app/create/1", True),
                          ("cleanup_orphan", "secret/2", True),
                          ("escalate", "secret/delete/2", True)])
        self.assertEqual(self.state("r1"), models.ESCALATED)
        self.assertEqual(self.owned("r1"), [])

    def test_nothing_below_a_failed_delete_is_touched_until_it_is_gone(self):
        self.provision([request("r1")], [fault("r1", "app", "quota")] +
                       [delete_fault("r1", "dns", "timeout", attempt=n) for n in (1, 2, 3)])
        self.run_agent()
        for step in ("secret", "database", "namespace"):
            self.assertEqual(self.delete_attempts("r1", step), [])

    def test_false_not_found_is_not_taken_as_a_successful_delete(self):
        self.provision([request("r1")], [fault("r1", "secret", "quota"),
                                         delete_fault("r1", "database", "duplicate")])
        self.run_agent()
        self.assertEqual(self.delete_attempts("r1", "database")[0],
                         (1, models.PRESENT, "not_found"))
        self.assertEqual(self.state("r1"), models.ESCALATED)

    def test_cleanup_budget_is_three_delete_attempts_then_nothing_is_claimed(self):
        self.provision([request("r1")], [fault("r1", "database", "quota")] +
                       [delete_fault("r1", "namespace", "timeout", attempt=n) for n in (1, 2, 3)])
        deleted = self.run_agent_spying_deletes()
        self.assertEqual(deleted, [self.name("r1", "namespace")] * 3)
        self.assertEqual([a[0] for a in self.delete_attempts("r1", "namespace")], [1, 2, 3])
        self.assertEqual(self.decisions("r1")[-2:],
                         [("cleanup_orphan", "namespace/4", False, "cleanup_budget_exhausted"),
                          ("escalate", "namespace/delete/3", False, "failed_deletion_pending")])
        self.assertEqual(self.state("r1"), models.CLEANUP_FAILED)
        self.assertEqual(self.owned("r1"), [self.name("r1", "namespace")])
        changes = [json.loads(e["details"])["to"] for e in self.reg.audit_events(
            models.env_id_for("r1")) if e["kind"] == "state_changed"]
        self.assertNotIn(models.ESCALATED, changes)


class OwnershipTest(CompensationTestCase):
    def test_mismatched_resource_is_never_deleted_and_blocks_escalation(self):
        env_id = models.env_id_for("r1")
        db = self.name("r1", "database")
        self.cloud.create(db, "dedicated_db", env_id, "database", None, 1)
        self.provision([request("r1", tier="basic")])
        deleted = self.run_agent_spying_deletes()

        self.assertNotIn(db, deleted)
        self.assertEqual(self.cloud.get(db)["kind"], "dedicated_db")
        self.assertEqual(self.state("r1"), models.CLEANUP_FAILED)
        self.assertEqual(self.owned("r1"), [db])
        self.assertEqual(self.decisions("r1")[-1],
                         ("escalate", "namespace/delete/1", False,
                          "unexpected_resource_needs_human"))

    def test_foreign_resource_is_never_deleted_and_does_not_block_escalation(self):
        dns = self.name("r1", "dns")
        other = models.env_id_for("other")
        self.cloud.create(dns, "dns", other, "dns", None, 1)
        self.provision([request("r1")])
        deleted = self.run_agent_spying_deletes()

        self.assertNotIn(dns, deleted)
        self.assertEqual(self.cloud.get(dns)["owner_env_id"], other)
        self.assertEqual(self.state("r1"), models.ESCALATED)
        self.assertEqual(self.owned("r1"), [])

    def test_resource_changed_under_us_is_not_deleted(self):
        self.provision([request("r1")], [fault("r1", "app", "quota")])
        env_id = models.env_id_for("r1")
        # Swap the dns resource for one with a different kind but the same owner.
        self.cloud.conn.execute("UPDATE inventory SET kind = 'app' WHERE name = ?",
                                (self.name("r1", "dns"),))
        deleted = self.run_agent_spying_deletes()
        self.assertNotIn(self.name("r1", "dns"), deleted)
        self.assertEqual(self.delete_attempts("r1", "dns"), [(1, models.MISMATCH, None)])
        self.assertEqual(self.state("r1"), models.CLEANUP_FAILED)
        self.assertIn(self.name("r1", "dns"), self.cloud.owned_by(env_id))

    def test_cleanup_policy_refuses_resources_that_are_not_ours_to_delete(self):
        env_id = models.env_id_for("r1")
        self.cloud.create(self.name("r1", "database"), "dedicated_db", env_id, "database",
                          None, 1)
        self.provision([request("r1", tier="basic")])
        self.run_agent()
        allowed, rule, _ = policy.evaluate(self.reg, env_id, "cleanup_orphan", "database/1")
        self.assertEqual((allowed, rule), (False, "resource_not_deletable"))
        # Already deleted by compensation.
        allowed, rule, _ = policy.evaluate(self.reg, env_id, "cleanup_orphan", "namespace/2")
        self.assertEqual((allowed, rule), (False, "resource_not_deletable"))
        # Never attempted, so the registry has no account of it.
        allowed, rule, _ = policy.evaluate(self.reg, env_id, "cleanup_orphan", "app/1")
        self.assertEqual((allowed, rule), (False, "resource_not_accounted"))


class ApprovalAndReportTest(CompensationTestCase):
    FAULTS = [fault("r1", "app", "quota"), delete_fault("r1", "secret", "timeout"),
              fault("r2", "database", "quota")] + \
             [delete_fault("r2", "namespace", "timeout", attempt=n) for n in (1, 2, 3)]

    def setUp(self):
        super().setUp()
        self.provision([request("r1"), request("r2", tier="enterprise")], self.FAULTS)
        self.run_agent()

    def test_every_delete_sits_inside_an_approved_escalate_or_cleanup(self):
        for env in self.reg.environments():
            open_decision = None
            for e in self.reg.audit_events(env["env_id"]):
                details = json.loads(e["details"])
                if e["kind"] == "policy_decision" and details["allowed"]:
                    open_decision = details["action"]
                elif e["kind"] == "action_completed":
                    open_decision = None
                elif e["kind"] == "op_intent" and e["ref"].split("/")[1] == "delete":
                    self.assertIn(open_decision, ("escalate", "cleanup_orphan"), e["ref"])

    def test_report_carries_evidence_and_actual_outcome(self):
        rows = {(r["request_id"], r["action"], r["target"]): r for r in agent.report(self.reg)}
        first = rows[("r1", "escalate", "app/create/1")]
        self.assertEqual(first["evidence"]["retry_rule"], "quota_not_retryable")
        self.assertEqual(sorted(first["evidence"]["present"]),
                         sorted(self.name("r1", s) for s in ("namespace", "database", "secret",
                                                             "dns")))
        self.assertEqual(first["outcome"]["env_state"], models.CLEANUP_FAILED)
        self.assertEqual([a["ref"] for a in first["outcome"]["attempts"]],
                         ["dns/delete/1", "secret/delete/1"])
        self.assertEqual(len(first["outcome"]["owned_resources"]), 3)

        last = rows[("r1", "escalate", "secret/delete/2")]
        self.assertEqual((last["outcome"]["env_state"], last["outcome"]["owned_resources"]),
                         (models.ESCALATED, []))

        denied = rows[("r2", "cleanup_orphan", "namespace/4")]
        self.assertEqual((denied["allowed"], denied["executed"], denied["outcome"]),
                         (False, False, None))
        self.assertEqual(denied["evidence"]["last_delete_attempt"], 3)

    def test_outcome_never_reports_escalated_while_resources_remain(self):
        for row in agent.report(self.reg):
            outcome = row["outcome"]
            if outcome and outcome["env_state"] == models.ESCALATED:
                self.assertEqual(outcome["owned_resources"], [], row["decision_id"])

    def test_agent_and_provision_reruns_write_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.run_agent(), [])
        workflow.provision(self.reg, self.cloud, self.requests)
        self.assertEqual(self.snapshot(), before)


class CrashMidCompensationTest(CompensationTestCase):
    def test_pending_escalate_resumes_with_open_delete(self):
        self.provision([request("r1")], [fault("r1", "app", "quota")])
        env_id = models.env_id_for("r1")
        policy.decide(self.reg, env_id, "retry_step", "app/2", DIGEST)
        decision = policy.decide(self.reg, env_id, "escalate", "app/create/1", DIGEST)
        # Simulate a crash after the cloud deleted dns but before the registry heard.
        self.reg.transition(env_id, models.COMPENSATING, "agent", reason="escalate app/create/1")
        self.reg.record_intent(env_id, "dns", "delete", 1, "agent")
        self.cloud.delete_dns(self.name("r1", "dns"), env_id, 1)

        deleted = self.run_agent_spying_deletes()
        self.assertNotIn(self.name("r1", "dns"), deleted)
        self.assertEqual(self.delete_attempts("r1", "dns"), [(1, models.ABSENT, None)])
        self.assertEqual(self.state("r1"), models.ESCALATED)
        self.assertTrue(self.reg.is_completed(decision["decision_id"]))


if __name__ == "__main__":
    unittest.main()
