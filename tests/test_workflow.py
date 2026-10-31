import json
import os
import tempfile
import unittest
from unittest import mock

from tenant_lifecycle import models, workflow
from tenant_lifecycle.cloud import Cloud, parse_faults
from tenant_lifecycle.registry import Registry


def request(request_id, tier="basic", region="eu-west-1", tenant="acme"):
    return {"request_id": request_id, "tenant": tenant, "tier": tier, "region": region}


def fault(request_id, step, kind, attempt=1):
    return {"request_id": request_id, "step": step, "op": "create", "attempt": attempt,
            "kind": kind}


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.open()

    def tearDown(self):
        self.reg.close()
        self.cloud.close()
        self.tmp.cleanup()

    def open(self, faults=()):
        self.reg = Registry(os.path.join(self.tmp.name, "registry.db"))
        self.cloud = Cloud(os.path.join(self.tmp.name, "inventory.db"), parse_faults(faults))

    def run_requests(self, requests, faults=()):
        self.cloud.faults = parse_faults(faults)
        workflow.provision(self.reg, self.cloud, list(enumerate(requests, start=1)))

    def env(self, request_id):
        return self.reg.get_environment(models.env_id_for(request_id))

    def snapshot(self):
        out = {t: [tuple(r) for r in self.reg.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
               for t in ("environments", "step_attempts", "resources", "audit")}
        out["inventory"] = [tuple(r) for r in self.cloud.inventory()]
        out["cloud_log"] = [tuple(r) for r in self.cloud.log()]
        return out

    def failure_reason(self, request_id):
        changes = [json.loads(e["details"]) for e in self.reg.audit_events(self.env(request_id)[0])
                   if e["kind"] == "state_changed"]
        self.assertEqual(changes[-1]["to"], models.STEP_FAILED)
        return changes[-1]["reason"]

    def create_calls(self, requests, faults=()):
        """Run the requests and return the names the workflow asked the cloud to create."""
        with mock.patch.object(self.cloud, "create", wraps=self.cloud.create) as spy:
            self.run_requests(requests, faults)
        return [c.args[0] for c in spy.call_args_list]

    def attempted_steps(self, request_id):
        return [a["step"] for a in self.reg.attempts(models.env_id_for(request_id))]


class TierTest(WorkflowTestCase):
    def test_basic_tier_reaches_active_with_five_matching_resources(self):
        self.run_requests([request("r1")])
        env_id = models.env_id_for("r1")
        self.assertEqual(self.env("r1")["state"], models.ACTIVE)
        self.assertEqual(
            [(r["owner_step"], r["kind"], r["parent"]) for r in self.cloud.inventory()],
            sorted([("namespace", "namespace", None),
                    ("database", "logical_db", "shared-cluster-eu-west-1"),
                    ("secret", "secret", None), ("dns", "dns", None), ("app", "app", None)],
                   key=lambda r: models.resource_name(env_id, r[0])))
        self.assertEqual({r["status"] for r in self.reg.resources(env_id)}, {models.PRESENT})

    def test_enterprise_tier_gets_a_dedicated_database(self):
        self.run_requests([request("r1", tier="enterprise", region="us-east-1")])
        self.assertEqual(self.env("r1")["state"], models.ACTIVE)
        db = self.cloud.get(models.resource_name(models.env_id_for("r1"), "database"))
        self.assertEqual((db["kind"], db["parent"]), ("dedicated_db", None))

    def test_audit_shows_intent_then_outcome_for_every_step(self):
        self.run_requests([request("r1")])
        events = self.reg.audit_events(models.env_id_for("r1"))
        self.assertEqual([e["kind"] for e in events],
                         ["request_registered", "state_changed"]
                         + ["op_intent", "op_outcome"] * 5 + ["state_changed"])
        self.assertEqual([e["ref"] for e in events if e["kind"] == "op_intent"],
                         [f"{s}/create/1" for s in models.STEPS])
        self.assertEqual({e["actor"] for e in events}, {"workflow"})

    def test_steps_and_requests_run_in_order(self):
        self.run_requests([request("r2", tier="enterprise"), request("r1")])
        names = [r["name"] for r in self.cloud.log()]
        expected = [models.resource_name(models.env_id_for(rid), s)
                    for rid in ("r2", "r1") for s in models.STEPS]
        self.assertEqual(names, expected)


class DuplicateRequestTest(WorkflowTestCase):
    def test_duplicate_request_id_provisions_once(self):
        self.run_requests([request("r1"), request("r2"), request("r1")])
        self.assertEqual(self.reg.state_counts(), {models.ACTIVE: 2})
        self.assertEqual(len(self.cloud.inventory()), 10)
        dup = [e for e in self.reg.audit_events(models.env_id_for("r1"))
               if e["kind"] == "duplicate_ignored"]
        self.assertEqual([e["ref"] for e in dup], ["line:3"])


class FailureTest(WorkflowTestCase):
    def test_timeout_stops_at_that_step_without_retry_or_delete(self):
        self.run_requests([request("r1")], [fault("r1", "secret", "timeout")])
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)
        self.assertEqual(self.failure_reason("r1"), "secret: absent (timeout)")
        self.assertEqual(sorted(self.attempted_steps("r1")), ["database", "namespace", "secret"])
        self.assertEqual(len(self.cloud.inventory()), 2)
        self.assertEqual({r["op"] for r in self.cloud.log()}, {"create"})

    def test_timeout_after_success_is_accepted_after_inventory_check(self):
        self.run_requests([request("r1")], [fault("r1", "secret", "timeout_after_success")])
        self.assertEqual(self.env("r1")["state"], models.ACTIVE)
        [secret] = [a for a in self.reg.attempts(models.env_id_for("r1")) if a["step"] == "secret"]
        self.assertEqual((secret["outcome"], secret["error_class"]), (models.PRESENT, None))
        self.assertEqual([r["response"] for r in self.cloud.log() if r["name"].endswith("-secret")],
                         ["timeout"])

    def test_quota_stops_the_environment(self):
        self.run_requests([request("r1", tier="enterprise")], [fault("r1", "database", "quota")])
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)
        self.assertEqual(self.failure_reason("r1"), "database: absent (quota)")
        self.assertEqual(len(self.cloud.inventory()), 1)

    def test_false_duplicate_response_is_not_taken_as_success(self):
        self.run_requests([request("r1")], [fault("r1", "dns", "duplicate")])
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)
        self.assertEqual(self.failure_reason("r1"), "dns: absent (already_exists)")

    def test_foreign_resource_is_reported_and_left_untouched(self):
        name = models.resource_name(models.env_id_for("r1"), "dns")
        other = models.env_id_for("someone-else")
        self.cloud.create(name, "dns", other, "dns", None, 1)

        self.assertNotIn(name, self.create_calls([request("r1")]))
        self.assertEqual(self.failure_reason("r1"), "dns: foreign_owner")
        self.assertEqual(self.cloud.get(name)["owner_env_id"], other)

    def test_mismatched_resource_is_reported_and_left_untouched(self):
        env_id = models.env_id_for("r1")
        name = models.resource_name(env_id, "database")
        self.cloud.create(name, "dedicated_db", env_id, "database", None, 1)

        self.assertNotIn(name, self.create_calls([request("r1", tier="basic")]))
        self.assertEqual(self.failure_reason("r1"), "database: mismatch")
        self.assertEqual(self.cloud.get(name)["kind"], "dedicated_db")
        self.assertEqual(sorted(self.attempted_steps("r1")), ["database", "namespace"])

    def test_failure_in_one_environment_does_not_stop_the_next(self):
        self.run_requests([request("r1"), request("r2")], [fault("r1", "namespace", "quota")])
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)
        self.assertEqual(self.env("r2")["state"], models.ACTIVE)

    def test_rerun_after_failure_does_not_retry(self):
        reqs, faults = [request("r1")], [fault("r1", "app", "timeout")]
        self.run_requests(reqs, faults)
        before = self.snapshot()
        self.assertEqual(self.create_calls(reqs, faults), [])
        self.assertEqual(self.snapshot(), before)


class ReplayTest(WorkflowTestCase):
    REQUESTS = [request("r1"), request("r2", tier="enterprise", region="us-east-1"),
                request("r1"), request("r3", region="us-east-1")]
    FAULTS = [fault("r1", "secret", "timeout_after_success"), fault("r2", "dns", "quota"),
              fault("r3", "database", "duplicate")]

    def test_rerun_on_the_same_files_writes_nothing(self):
        self.run_requests(self.REQUESTS, self.FAULTS)
        before = self.snapshot()
        self.reg.close()
        self.cloud.close()
        self.open()
        self.run_requests(self.REQUESTS, self.FAULTS)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_runs_are_identical(self):
        self.run_requests(self.REQUESTS, self.FAULTS)
        first = self.snapshot()
        self.reg.close()
        self.cloud.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.open()
        self.run_requests(self.REQUESTS, self.FAULTS)
        self.assertEqual(self.snapshot(), first)

    def test_every_inventory_resource_has_a_registry_record(self):
        self.run_requests(self.REQUESTS, self.FAULTS)
        accounted = {r[0] for r in self.reg.conn.execute("SELECT name FROM resources")}
        self.assertLessEqual({r["name"] for r in self.cloud.inventory()}, accounted)


class ReconcileTest(WorkflowTestCase):
    def start(self, request_id="r1"):
        req = request(request_id)
        env_id, _ = self.reg.register_request(request_id, 1, "acme", "basic", "eu-west-1", req)
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        return env_id, req

    def test_open_attempt_with_resource_present_is_recorded_without_a_new_call(self):
        env_id, req = self.start()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.cloud.create_namespace(models.resource_name(env_id, "namespace"), env_id, 1)

        calls = self.create_calls([req])
        self.assertEqual(self.env("r1")["state"], models.ACTIVE)
        self.assertEqual(calls, [models.resource_name(env_id, s) for s in models.STEPS[1:]])

    def test_open_attempt_with_nothing_present_resends_the_same_attempt(self):
        env_id, req = self.start()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        calls = self.create_calls([req], [fault("r1", "namespace", "quota", attempt=1)])
        self.assertEqual(calls, [models.resource_name(env_id, "namespace")])
        [ns] = [a for a in self.reg.attempts(env_id) if a["step"] == "namespace"]
        self.assertEqual((ns["attempt"], ns["outcome"], ns["error_class"]),
                         (1, models.ABSENT, "quota"))
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)

    def test_recorded_failure_without_transition_is_finished_without_a_call(self):
        env_id, req = self.start()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "timeout",
                                "workflow")
        self.assertEqual(self.create_calls([req]), [])
        self.assertEqual(self.env("r1")["state"], models.STEP_FAILED)
        self.assertEqual(self.cloud.log(), [])


class ParseRequestsTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "requests.jsonl")
            with open(path, "w") as f:
                f.write(text)
            return workflow.parse_requests(path)

    def test_blank_lines_are_skipped_and_line_numbers_are_physical(self):
        line = json.dumps(request("r1"))
        self.assertEqual([n for n, _ in self.parse(f"{line}\n\n{line}\n")], [1, 3])

    def test_invalid_lines_are_rejected(self):
        bad = {
            "invalid json": "{not json",
            "not an object": "[1, 2]",
            "missing field": json.dumps({"request_id": "r1", "tenant": "a", "tier": "basic"}),
            "empty field": json.dumps(request("")),
            "non-string field": json.dumps(dict(request("r1"), region=5)),
            "unknown tier": json.dumps(request("r1", tier="gold")),
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(workflow.RequestFileError):
                self.parse(text + "\n")


if __name__ == "__main__":
    unittest.main()
