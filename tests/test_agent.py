import ast
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from tenant_lifecycle import agent, models, policy, workflow
from tenant_lifecycle.cloud import Cloud, parse_faults
from tenant_lifecycle.registry import Registry


RULES, DIGEST = agent.load_rules()


def request(request_id, tier="basic"):
    return {"request_id": request_id, "tenant": "acme", "tier": tier, "region": "eu-west-1"}


def fault(request_id, step, kind, attempt=1):
    return {"request_id": request_id, "step": step, "op": "create", "attempt": attempt,
            "kind": kind}


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Registry(os.path.join(self.tmp.name, "registry.db"))
        self.cloud = Cloud(os.path.join(self.tmp.name, "inventory.db"), {})

    def tearDown(self):
        self.reg.close()
        self.cloud.close()
        self.tmp.cleanup()

    def provision(self, requests, faults=()):
        self.requests = list(enumerate(requests, start=1))
        self.cloud.faults = parse_faults(faults)
        workflow.provision(self.reg, self.cloud, self.requests)

    def run_agent(self):
        executed = []

        def execute(decision_id):
            # Record what was approved at the moment of execution, before it runs.
            decision = self.reg.get_decision(decision_id)
            executed.append((decision_id, bool(decision and decision["allowed"])))
            workflow.execute(self.reg, self.cloud, decision_id)

        agent.run(self.reg, execute, RULES, DIGEST)
        return executed

    def state(self, request_id):
        return self.reg.get_environment(models.env_id_for(request_id))["state"]

    def attempts(self, request_id, step):
        return [(a["attempt"], a["actor"], a["outcome"], a["error_class"])
                for a in self.reg.attempts(models.env_id_for(request_id)) if a["step"] == step]

    def decisions(self, request_id):
        env_id = models.env_id_for(request_id)
        return [(d["action"], d["target"], bool(d["allowed"]), d["rule"])
                for d in self.reg.decisions() if d["env_id"] == env_id]

    def snapshot(self):
        out = {t: self.reg.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
               for t in ("environments", "step_attempts", "resources", "audit",
                         "policy_decisions")}
        out["inventory"] = self.cloud.inventory()
        out["cloud_log"] = self.cloud.log()
        return {k: [tuple(r) for r in v] for k, v in out.items()}


class RetryAndResumeTest(AgentTestCase):
    def test_retry_then_resume_reaches_active(self):
        self.provision([request("r1")], [fault("r1", "secret", "timeout")])
        self.run_agent()
        self.assertEqual(self.state("r1"), models.ACTIVE)
        self.assertEqual(self.attempts("r1", "secret"),
                         [(1, "workflow", "absent", "timeout"), (2, "agent", "present", None)])
        self.assertEqual(self.decisions("r1"),
                         [("retry_step", "secret/2", True, "retry_allowed"),
                          ("resume", "secret/2", True, "resume_allowed")])
        self.assertEqual([a[1] for a in self.attempts("r1", "dns")], ["agent"])

    def test_a_later_step_failing_during_resume_gets_its_own_budget(self):
        self.provision([request("r1")],
                       [fault("r1", "namespace", "duplicate"), fault("r1", "app", "timeout"),
                        fault("r1", "app", "timeout", attempt=2)])
        self.run_agent()
        self.assertEqual(self.state("r1"), models.ACTIVE)
        self.assertEqual([d[:2] for d in self.decisions("r1")],
                         [("retry_step", "namespace/2"), ("resume", "namespace/2"),
                          ("retry_step", "app/2"), ("retry_step", "app/3"), ("resume", "app/3")])

    def test_quota_is_denied_and_never_retried(self):
        self.provision([request("r1", tier="enterprise")], [fault("r1", "database", "quota")])
        with mock.patch.object(self.cloud, "create", wraps=self.cloud.create) as spy:
            self.run_agent()
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.state("r1"), models.ESCALATED)
        self.assertEqual(self.decisions("r1"),
                         [("retry_step", "database/2", False, "quota_not_retryable"),
                          ("escalate", "database/create/1", True, "escalate_allowed")])
        self.assertEqual(self.cloud.owned_by(models.env_id_for("r1")), [])

    def test_retries_stop_after_two_with_three_attempts_in_total(self):
        self.provision([request("r1")],
                       [fault("r1", "dns", "timeout", attempt=n) for n in (1, 2, 3)])
        with mock.patch.object(self.cloud, "create", wraps=self.cloud.create) as spy:
            self.run_agent()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual([a[0] for a in self.attempts("r1", "dns")], [1, 2, 3])
        self.assertEqual(self.decisions("r1")[-2:],
                         [("retry_step", "dns/4", False, "retry_budget_exhausted"),
                          ("escalate", "dns/create/3", True, "escalate_allowed")])
        self.assertEqual(self.state("r1"), models.ESCALATED)

    def test_foreign_owner_is_not_retried(self):
        env_id = models.env_id_for("r1")
        name = models.resource_name(env_id, "dns")
        self.cloud.create(name, "dns", models.env_id_for("other"), "dns", None, 1)
        self.provision([request("r1")])
        self.run_agent()
        self.assertEqual(self.decisions("r1"),
                         [("retry_step", "dns/2", False, "outcome_not_retryable"),
                          ("escalate", "dns/create/1", True, "escalate_allowed")])
        self.assertEqual(self.cloud.get(name)["owner_env_id"], models.env_id_for("other"))

    def test_resume_of_a_crashed_provision_checks_the_inventory_before_calling(self):
        req = request("r1")
        env_id, _ = self.reg.register_request("r1", 1, "acme", "basic", "eu-west-1", req)
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        name = models.resource_name(env_id, "namespace")
        self.cloud.create_namespace(name, env_id, 1)  # the call landed; the crash came first

        with mock.patch.object(self.cloud, "create", wraps=self.cloud.create) as spy:
            self.run_agent()
        self.assertNotIn(name, [c.args[0] for c in spy.call_args_list])
        self.assertEqual(self.attempts("r1", "namespace"), [(1, "workflow", "present", None)])
        self.assertEqual(self.state("r1"), models.ACTIVE)
        self.assertEqual(self.decisions("r1"), [("resume", "namespace/1", True, "resume_allowed")])

    def test_active_environments_are_left_alone(self):
        self.provision([request("r1")])
        self.assertEqual(self.run_agent(), [])
        self.assertEqual(self.reg.decisions(), [])


class AgentBoundaryTest(AgentTestCase):
    def test_agent_module_has_no_access_to_the_cloud(self):
        tree = ast.parse(inspect.getsource(agent))
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("cloud", imported)
        self.assertFalse(any("cloud" in (m or "") for m in modules))
        self.assertNotIn("cloud", inspect.signature(agent.run).parameters)

    def test_only_approved_decisions_reach_the_executor(self):
        self.provision([request("r1"), request("r2", tier="enterprise")],
                       [fault("r1", "secret", "timeout"), fault("r2", "database", "quota")])
        executed = self.run_agent()
        self.assertTrue(executed)
        self.assertTrue(all(approved for _, approved in executed))
        denied = {d["decision_id"] for d in self.reg.decisions() if not d["allowed"]}
        self.assertTrue(denied)
        self.assertFalse(denied & {decision_id for decision_id, _ in executed})

    def test_agent_proposes_only_allowlisted_actions(self):
        self.provision([request("r1"), request("r2"), request("r3")],
                       [fault("r1", "secret", "timeout"), fault("r2", "app", "quota"),
                        fault("r3", "dns", "duplicate")])
        self.run_agent()
        actions = {d["action"] for d in self.reg.decisions()}
        self.assertLessEqual(actions, {"retry_step", "resume", "cleanup_orphan", "escalate"})

    def test_executor_that_does_not_complete_stops_the_agent(self):
        self.provision([request("r1")], [fault("r1", "secret", "timeout")])
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            agent.run(self.reg, lambda decision_id: None, RULES, DIGEST)


class AuditOrderingTest(AgentTestCase):
    def test_every_agent_change_sits_inside_an_approved_decision(self):
        self.provision([request("r1"), request("r2", tier="enterprise"), request("r3")],
                       [fault("r1", "secret", "timeout"), fault("r2", "database", "quota"),
                        fault("r3", "namespace", "duplicate"), fault("r3", "app", "timeout")])
        self.run_agent()
        for env in self.reg.environments():
            open_decision = None
            for e in self.reg.audit_events(env["env_id"]):
                details = json.loads(e["details"])
                if e["kind"] == "policy_decision":
                    self.assertIsNone(open_decision, "decision made while another is running")
                    if details["allowed"]:
                        open_decision = e["ref"]
                elif e["kind"] == "action_completed":
                    self.assertEqual(e["ref"], open_decision)
                    open_decision = None
                elif e["actor"] == "agent":
                    self.assertIsNotNone(open_decision, f"unapproved agent change {e['kind']}")
            self.assertIsNone(open_decision, "approved action never completed")

    def test_intent_precedes_outcome_for_agent_attempts(self):
        self.provision([request("r1")], [fault("r1", "secret", "timeout")])
        self.run_agent()
        kinds = [(e["kind"], e["ref"]) for e in self.reg.audit_events(models.env_id_for("r1"))
                 if e["ref"] == "secret/create/2"]
        self.assertEqual(kinds, [("op_intent", "secret/create/2"),
                                 ("op_outcome", "secret/create/2")])


class AgentReplayTest(AgentTestCase):
    FAULTS = [fault("r1", "secret", "timeout"), fault("r2", "database", "quota"),
              fault("r3", "dns", "timeout", attempt=1), fault("r3", "dns", "timeout", attempt=2),
              fault("r3", "dns", "timeout", attempt=3)]

    def setUp(self):
        super().setUp()
        self.provision([request("r1"), request("r2", tier="enterprise"), request("r3")],
                       self.FAULTS)
        self.run_agent()

    def test_agent_rerun_writes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.run_agent(), [])
        self.assertEqual(self.snapshot(), before)

    def test_provision_rerun_leaves_agent_owned_environments_alone(self):
        before = self.snapshot()
        workflow.provision(self.reg, self.cloud, self.requests)
        self.assertEqual(self.snapshot(), before)

    def test_pending_action_from_a_crash_is_finished_first(self):
        env_id = models.env_id_for("r4")
        self.cloud.faults = parse_faults([fault("r4", "secret", "timeout")])
        workflow.provision(self.reg, self.cloud, [(4, request("r4"))])
        # Simulate a crash right after the approval was stored.
        decision = policy.decide(self.reg, env_id, "retry_step", "secret/2", DIGEST)
        executed = self.run_agent()
        self.assertEqual(executed[0], (decision["decision_id"], True))
        self.assertEqual(self.state("r4"), models.ACTIVE)

    def test_report_lists_every_decision_in_order(self):
        report = agent.report(self.reg)
        self.assertEqual([(r["request_id"], r["action"], r["target"], r["allowed"],
                           r["executed"]) for r in report],
                         [("r1", "retry_step", "secret/2", True, True),
                          ("r1", "resume", "secret/2", True, True),
                          ("r2", "retry_step", "database/2", False, False),
                          ("r2", "escalate", "database/create/1", True, True),
                          ("r3", "retry_step", "dns/2", True, True),
                          ("r3", "retry_step", "dns/3", True, True),
                          ("r3", "retry_step", "dns/4", False, False),
                          ("r3", "escalate", "dns/create/3", True, True)])


if __name__ == "__main__":
    unittest.main()
