import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from tenant_lifecycle import models
from tenant_lifecycle.registry import IllegalTransition, Registry, RegistryConflict

REGION = "eu-west-1"
PAYLOAD = {"request_id": "req-1", "tenant": "acme", "tier": "basic", "region": REGION}


def snapshot(reg):
    """Every row of every table, for before/after comparisons."""
    tables = ("environments", "step_attempts", "resources", "audit")
    return {t: [tuple(r) for r in reg.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in tables}


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "registry.db")
        self.reg = Registry(self.path)

    def tearDown(self):
        self.reg.close()
        self.tmp.cleanup()

    def new_env(self, request_id="req-1", line_no=1, tier="basic"):
        payload = dict(PAYLOAD, request_id=request_id, tier=tier)
        env_id, status = self.reg.register_request(request_id, line_no, "acme", tier, REGION,
                                                   payload)
        self.assertEqual(status, "created")
        return env_id

    def provisioning_env(self):
        env_id = self.new_env()
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        return env_id

    def kinds(self, env_id):
        return [e["kind"] for e in self.reg.audit_events(env_id)]


class TransitionTest(RegistryTestCase):
    def test_legal_path_is_applied_and_audited(self):
        env_id = self.new_env()
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.reg.transition(env_id, models.ACTIVE, "workflow", reason="all steps present")

        self.assertEqual(self.reg.get_environment(env_id)["state"], models.ACTIVE)
        changes = [json.loads(e["details"]) for e in self.reg.audit_events(env_id)
                   if e["kind"] == "state_changed"]
        self.assertEqual([(c["from"], c["to"]) for c in changes],
                         [(models.PENDING, models.PROVISIONING),
                          (models.PROVISIONING, models.ACTIVE)])
        self.assertEqual(changes[1]["reason"], "all steps present")

    def test_illegal_transition_changes_nothing(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        with self.assertRaises(IllegalTransition):
            self.reg.transition(env_id, models.ACTIVE, "workflow")
        self.assertEqual(snapshot(self.reg), before)

    def test_terminal_state_has_no_exit(self):
        env_id = self.provisioning_env()
        self.reg.transition(env_id, models.ACTIVE, "workflow")
        for target in models.STATES:
            with self.assertRaises(IllegalTransition):
                self.reg.transition(env_id, target, "agent")

    def test_unknown_actor_is_rejected_before_any_write(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        with self.assertRaises(ValueError):
            self.reg.transition(env_id, models.PROVISIONING, "robot")
        self.assertEqual(snapshot(self.reg), before)

    def test_transition_waits_for_unresolved_attempt(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        before = snapshot(self.reg)
        with self.assertRaisesRegex(RegistryConflict, "namespace/create/1 is unresolved"):
            self.reg.transition(env_id, models.STEP_FAILED, "workflow")
        self.assertEqual(snapshot(self.reg), before)

        self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "timeout",
                                "workflow")
        self.reg.transition(env_id, models.STEP_FAILED, "workflow")
        self.assertEqual(self.reg.get_environment(env_id)["state"], models.STEP_FAILED)

    def test_unresolved_attempt_of_another_environment_does_not_block(self):
        a = self.provisioning_env()
        b = self.new_env("req-b", 2)
        self.reg.record_intent(a, "namespace", "create", 1, "workflow")
        self.reg.transition(b, models.PROVISIONING, "workflow")
        self.assertEqual(self.reg.get_environment(b)["state"], models.PROVISIONING)

    def test_unknown_environment_is_rejected(self):
        with self.assertRaises(KeyError):
            self.reg.transition("env-missing", models.PROVISIONING, "workflow")


class RequestIdempotencyTest(RegistryTestCase):
    def test_replaying_the_same_line_writes_nothing(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        self.assertEqual(self.reg.register_request("req-1", 1, "acme", "basic", REGION, PAYLOAD),
                         (env_id, "existing"))
        self.assertEqual(snapshot(self.reg), before)

    def test_duplicate_request_id_never_creates_second_environment(self):
        env_id = self.new_env()
        self.assertEqual(self.reg.register_request("req-1", 7, "acme", "basic", REGION, PAYLOAD),
                         (env_id, "duplicate"))
        (count,) = self.reg.conn.execute("SELECT COUNT(*) FROM environments").fetchone()
        self.assertEqual(count, 1)
        dup = [e for e in self.reg.audit_events(env_id) if e["kind"] == "duplicate_ignored"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["ref"], "line:7")
        self.assertEqual(json.loads(dup[0]["details"]),
                         {"request_id": "req-1", "original_line": 1, "same_payload": True})

    def test_duplicate_is_audited_once_across_replays(self):
        self.new_env()
        self.reg.register_request("req-1", 7, "acme", "basic", REGION, PAYLOAD)
        before = snapshot(self.reg)
        self.assertEqual(self.reg.register_request("req-1", 7, "acme", "basic", REGION, PAYLOAD)[1],
                         "duplicate")
        self.assertEqual(snapshot(self.reg), before)

    def test_duplicate_with_different_payload_is_flagged_not_applied(self):
        env_id = self.new_env()
        other = dict(PAYLOAD, tier="enterprise")
        self.reg.register_request("req-1", 9, "acme", "enterprise", REGION, other)
        self.assertEqual(self.reg.get_environment(env_id)["tier"], "basic")
        dup = [e for e in self.reg.audit_events(env_id) if e["kind"] == "duplicate_ignored"]
        self.assertFalse(json.loads(dup[0]["details"])["same_payload"])

    def test_same_line_with_changed_payload_is_a_conflict(self):
        self.new_env()
        with self.assertRaises(RegistryConflict):
            self.reg.register_request("req-1", 1, "x", "basic", REGION,
                                      dict(PAYLOAD, tenant="x"))

    def test_unknown_tier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown tier"):
            self.reg.register_request("req-9", 1, "acme", "gold", REGION,
                                      dict(PAYLOAD, request_id="req-9", tier="gold"))

    def test_full_replay_after_reopening_the_file_writes_nothing(self):
        env_id = self.provisioning_env()
        self.reg.register_request("req-1", 4, "acme", "basic", REGION, PAYLOAD)
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        before = snapshot(self.reg)
        self.reg.close()

        self.reg = Registry(self.path)
        self.reg.register_request("req-1", 1, "acme", "basic", REGION, PAYLOAD)
        self.reg.register_request("req-1", 4, "acme", "basic", REGION, PAYLOAD)
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        self.assertEqual(snapshot(self.reg), before)


class RegionTest(RegistryTestCase):
    def test_region_is_stored_and_audited_at_registration(self):
        env_id = self.new_env()
        self.assertEqual(self.reg.get_environment(env_id)["region"], REGION)
        [registered] = [e for e in self.reg.audit_events(env_id)
                        if e["kind"] == "request_registered"]
        self.assertEqual(json.loads(registered["details"])["region"], REGION)

    def test_region_survives_replay_after_reopening_the_file(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        self.reg.close()

        self.reg = Registry(self.path)
        self.assertEqual(self.reg.register_request("req-1", 1, "acme", "basic", REGION, PAYLOAD),
                         (env_id, "existing"))
        self.assertEqual(self.reg.get_environment(env_id)["region"], REGION)
        self.assertEqual(snapshot(self.reg), before)

    def test_replayed_line_with_changed_region_is_a_conflict(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        moved = dict(PAYLOAD, region="us-east-1")
        with self.assertRaises(RegistryConflict):
            self.reg.register_request("req-1", 1, "acme", "basic", "us-east-1", moved)
        self.assertEqual(self.reg.get_environment(env_id)["region"], REGION)
        self.assertEqual(snapshot(self.reg), before)

    def test_duplicate_with_other_region_does_not_move_the_environment(self):
        env_id = self.new_env()
        moved = dict(PAYLOAD, region="us-east-1")
        self.assertEqual(self.reg.register_request("req-1", 5, "acme", "basic", "us-east-1", moved),
                         (env_id, "duplicate"))
        self.assertEqual(self.reg.get_environment(env_id)["region"], REGION)
        [dup] = [e for e in self.reg.audit_events(env_id) if e["kind"] == "duplicate_ignored"]
        self.assertFalse(json.loads(dup["details"])["same_payload"])

    def test_missing_region_is_rejected_before_any_write(self):
        before = snapshot(self.reg)
        for bad in ("", None):
            payload = dict(PAYLOAD, request_id="req-2", region=bad)
            with self.assertRaisesRegex(ValueError, "region must be"):
                self.reg.register_request("req-2", 2, "acme", "basic", bad, payload)
        self.assertEqual(snapshot(self.reg), before)

    def test_basic_database_uses_its_regions_shared_cluster_and_no_extra_resource(self):
        payload = dict(PAYLOAD, request_id="req-us", region="us-east-1")
        env_id, _ = self.reg.register_request("req-us", 3, "acme", "basic", "us-east-1", payload)
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.reg.record_intent(env_id, "database", "create", 1, "workflow")

        [res] = self.reg.resources(env_id)
        self.assertEqual((res["kind"], res["parent"]), ("logical_db", "shared-cluster-us-east-1"))
        (total,) = self.reg.conn.execute("SELECT COUNT(*) FROM resources").fetchone()
        self.assertEqual(total, 1)

    def test_enterprise_database_has_no_shared_cluster(self):
        env_id = self.new_env(tier="enterprise")
        self.reg.transition(env_id, models.PROVISIONING, "workflow")
        self.reg.record_intent(env_id, "database", "create", 1, "workflow")
        [res] = self.reg.resources(env_id)
        self.assertEqual((res["kind"], res["parent"]), ("dedicated_db", None))


class PayloadValidationTest(RegistryTestCase):
    ARGS = {"request_id": "req-1", "tenant": "acme", "tier": "basic", "region": REGION}

    def register(self, payload, **overrides):
        args = dict(self.ARGS, **overrides)
        return self.reg.register_request(args["request_id"], 1, args["tenant"], args["tier"],
                                         args["region"], payload)

    def test_each_field_must_match_the_payload(self):
        before = snapshot(self.reg)
        mismatches = {"request_id": "req-other", "tenant": "globex", "tier": "enterprise",
                      "region": "us-east-1"}
        for key, other in mismatches.items():
            with self.subTest(field=key):
                with self.assertRaisesRegex(ValueError, f"^{key} "):
                    self.register(dict(PAYLOAD, **{key: other}))
        self.assertEqual(snapshot(self.reg), before)

    def test_missing_payload_field_is_rejected(self):
        before = snapshot(self.reg)
        for key in self.ARGS:
            with self.subTest(field=key):
                payload = {k: v for k, v in PAYLOAD.items() if k != key}
                with self.assertRaisesRegex(ValueError, f"^{key} "):
                    self.register(payload)
        self.assertEqual(snapshot(self.reg), before)

    def test_non_object_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self.register(["req-1", "acme"])

    def test_mismatched_duplicate_is_rejected_not_audited(self):
        env_id = self.new_env()
        before = snapshot(self.reg)
        with self.assertRaises(ValueError):
            self.reg.register_request("req-1", 8, "acme", "basic", REGION,
                                      dict(PAYLOAD, tenant="globex"))
        self.assertEqual(snapshot(self.reg), before)
        self.assertEqual(self.reg.get_environment(env_id)["tenant"], "acme")


class OperationTrackingTest(RegistryTestCase):
    def test_intent_opens_attempt_and_accounts_for_resource(self):
        env_id = self.provisioning_env()
        self.assertTrue(self.reg.record_intent(env_id, "database", "create", 1, "workflow"))

        [attempt] = self.reg.open_attempts(env_id)
        self.assertEqual((attempt["step"], attempt["op"], attempt["attempt"]),
                         ("database", "create", 1))
        self.assertIsNone(attempt["outcome"])
        [res] = self.reg.resources(env_id)
        self.assertEqual((res["name"], res["kind"], res["parent"], res["status"]),
                         (models.resource_name(env_id, "database"), "logical_db",
                          models.shared_cluster(REGION), "intended"))

    def test_intent_and_outcome_are_distinct_audit_records(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "timeout",
                                "workflow")

        events = [e for e in self.reg.audit_events(env_id) if e["kind"].startswith("op_")]
        self.assertEqual([(e["kind"], e["ref"]) for e in events],
                         [("op_intent", "namespace/create/1"), ("op_outcome", "namespace/create/1")])
        self.assertNotIn("outcome", json.loads(events[0]["details"]))
        self.assertEqual(json.loads(events[1]["details"])["outcome"], models.ABSENT)
        self.assertEqual(json.loads(events[1]["details"])["error_class"], "timeout")
        self.assertEqual(self.reg.open_attempts(env_id), [])

    def test_repeated_intent_or_outcome_writes_nothing(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        before = snapshot(self.reg)
        self.assertFalse(self.reg.record_intent(env_id, "namespace", "create", 1, "workflow"))
        self.assertEqual(snapshot(self.reg), before)

        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        before = snapshot(self.reg)
        self.assertFalse(self.reg.record_outcome(env_id, "namespace", "create", 1,
                                                 models.PRESENT, None, "workflow"))
        self.assertEqual(snapshot(self.reg), before)

    def test_conflicting_outcome_is_rejected(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        with self.assertRaises(RegistryConflict):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "quota",
                                    "workflow")

    def test_outcome_without_intent_is_rejected(self):
        env_id = self.provisioning_env()
        with self.assertRaises(RegistryConflict):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None,
                                    "workflow")

    def test_only_one_open_attempt_per_step_and_op(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "create", 2, "workflow")

    def test_attempts_must_be_sequential(self):
        env_id = self.provisioning_env()
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "create", 2, "workflow")
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "transient",
                                "workflow")
        self.assertEqual(self.reg.next_attempt(env_id, "namespace", "create"), 2)
        self.assertTrue(self.reg.record_intent(env_id, "namespace", "create", 2, "workflow"))
        [res] = self.reg.resources(env_id)
        self.assertEqual(res["status"], "intended")

    def test_error_class_only_when_call_changed_nothing(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        with self.assertRaises(ValueError):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, "timeout",
                                    "workflow")
        with self.assertRaises(ValueError):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, None,
                                    "workflow")

    def test_mismatch_outcome_is_recorded_without_error_class(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "database", "create", 1, "workflow")
        with self.assertRaises(ValueError):
            self.reg.record_outcome(env_id, "database", "create", 1, models.MISMATCH,
                                    "already_exists", "workflow")
        self.reg.record_outcome(env_id, "database", "create", 1, models.MISMATCH, None,
                                "workflow")
        [res] = self.reg.resources(env_id)
        self.assertEqual(res["status"], models.MISMATCH)
        self.assertEqual(self.reg.open_attempts(env_id), [])

    def test_create_only_while_provisioning(self):
        env_id = self.new_env()
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")

    def test_create_refused_when_resource_already_present(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "create", 2, "workflow")

    def test_delete_requires_accounted_resource_and_cleanup_state(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "delete", 1, "agent")

        self.reg.transition(env_id, models.STEP_FAILED, "workflow")
        self.reg.transition(env_id, models.COMPENSATING, "agent")
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "dns", "delete", 1, "agent")

        self.reg.record_intent(env_id, "namespace", "delete", 1, "agent")
        self.reg.record_outcome(env_id, "namespace", "delete", 1, models.ABSENT, None, "agent")
        [res] = self.reg.resources(env_id)
        self.assertEqual(res["status"], models.ABSENT)

    def test_failed_delete_keeps_resource_present(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "workflow")
        self.reg.transition(env_id, models.STEP_FAILED, "workflow")
        self.reg.transition(env_id, models.COMPENSATING, "agent")
        self.reg.record_intent(env_id, "namespace", "delete", 1, "agent")
        self.reg.record_outcome(env_id, "namespace", "delete", 1, models.PRESENT, "delete_failed",
                                "agent")
        [res] = self.reg.resources(env_id)
        self.assertEqual(res["status"], models.PRESENT)

    def test_attempt_belongs_to_its_actor(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        with self.assertRaises(RegistryConflict):
            self.reg.record_intent(env_id, "namespace", "create", 1, "agent")
        with self.assertRaises(RegistryConflict):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None, "human")

    def test_agent_may_resolve_an_attempt_a_crashed_provision_left_open(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.assertTrue(self.reg.record_outcome(env_id, "namespace", "create", 1,
                                                models.PRESENT, None, "agent"))
        [attempt] = self.reg.attempts(env_id)
        self.assertEqual(attempt["actor"], "workflow")
        outcome = [e for e in self.reg.audit_events(env_id) if e["kind"] == "op_outcome"]
        self.assertEqual([e["actor"] for e in outcome], ["agent"])

    def test_workflow_may_not_resolve_an_agent_attempt(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "agent")
        with self.assertRaises(RegistryConflict):
            self.reg.record_outcome(env_id, "namespace", "create", 1, models.PRESENT, None,
                                    "workflow")


class AuditConsistencyTest(RegistryTestCase):
    def test_failed_audit_insert_rolls_back_the_change(self):
        env_id = self.provisioning_env()
        before = snapshot(self.reg)
        with mock.patch.object(self.reg, "_audit", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.reg.transition(env_id, models.ACTIVE, "workflow")
            with self.assertRaises(RuntimeError):
                self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.assertEqual(snapshot(self.reg), before)

    def test_audit_is_append_only(self):
        env_id = self.new_env()
        with self.assertRaises(sqlite3.IntegrityError):
            self.reg.conn.execute("UPDATE audit SET actor = 'human' WHERE env_id = ?", (env_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.reg.conn.execute("DELETE FROM audit WHERE env_id = ?", (env_id,))

    def test_database_rejects_a_second_intent_record_for_one_attempt(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reg._audit(env_id, "workflow", "op_intent", "namespace/create/1", {})

    def test_every_change_has_exactly_one_audit_record_in_order(self):
        env_id = self.provisioning_env()
        self.reg.record_intent(env_id, "namespace", "create", 1, "workflow")
        self.reg.record_outcome(env_id, "namespace", "create", 1, models.ABSENT, "transient",
                                "workflow")
        self.reg.transition(env_id, models.STEP_FAILED, "workflow")
        self.reg.transition(env_id, models.PROVISIONING, "agent", reason="retry_step")
        self.reg.record_intent(env_id, "namespace", "create", 2, "agent")
        self.reg.record_outcome(env_id, "namespace", "create", 2, models.PRESENT, None, "agent")

        events = self.reg.audit_events(env_id)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(self.kinds(env_id),
                         ["request_registered", "state_changed", "op_intent", "op_outcome",
                          "state_changed", "state_changed", "op_intent", "op_outcome"])
        self.assertEqual([e["actor"] for e in events][-3:], ["agent", "agent", "agent"])

        last_change = json.loads([e for e in events if e["kind"] == "state_changed"][-1]["details"])
        self.assertEqual(last_change["to"], self.reg.get_environment(env_id)["state"])
        for a in self.reg.attempts(env_id):
            ref = f"{a['step']}/{a['op']}/{a['attempt']}"
            self.assertEqual([e["kind"] for e in events if e["ref"] == ref],
                             ["op_intent", "op_outcome"])

    def test_audit_sequences_are_per_environment(self):
        a = self.new_env("req-a", 1)
        b = self.new_env("req-b", 2)
        self.reg.transition(a, models.PROVISIONING, "workflow")
        self.assertEqual([e["seq"] for e in self.reg.audit_events(a)], [1, 2])
        self.assertEqual([e["seq"] for e in self.reg.audit_events(b)], [1])


if __name__ == "__main__":
    unittest.main()
