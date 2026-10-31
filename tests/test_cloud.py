import json
import os
import tempfile
import unittest
from unittest import mock

from tenant_lifecycle import models, writes
from tenant_lifecycle.cloud import (
    AlreadyExists, Cloud, CloudTimeout, NotFound, OwnershipMismatch, QuotaExceeded,
    load_faults, parse_faults,
)

REQUEST_ID = "req-1"
ENV = models.env_id_for(REQUEST_ID)
OTHER_ENV = models.env_id_for("req-2")
REGION = "eu-west-1"


def fault(kind, step="namespace", op="create", attempt=1, request_id=REQUEST_ID):
    return {"request_id": request_id, "step": step, "op": op, "attempt": attempt, "kind": kind}


class CloudTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "inventory.db")
        self.cloud = Cloud(self.path)

    def tearDown(self):
        self.cloud.close()
        self.tmp.cleanup()

    def with_faults(self, *records):
        self.cloud.faults = parse_faults(records)

    def create(self, step="namespace", env_id=ENV, tier="basic", attempt=1):
        name = models.resource_name(env_id, step)
        self.cloud.create(name, models.resource_kind(step, tier), env_id, step,
                          models.resource_parent(step, tier, REGION), attempt)
        return name

    def delete(self, step="namespace", env_id=ENV, attempt=1, name=None):
        name = name or models.resource_name(env_id, step)
        self.cloud.delete(name, env_id, step, attempt)
        return name

    def state(self):
        return ([tuple(r) for r in self.cloud.inventory()], [tuple(r) for r in self.cloud.log()])


class CreateDeleteTest(CloudTestCase):
    def test_every_step_and_tier_can_be_created_and_deleted(self):
        for tier in models.TIERS:
            for step in models.STEPS:
                with self.subTest(tier=tier, step=step):
                    env_id = models.env_id_for(f"req-{tier}")
                    name = self.create(step, env_id=env_id, tier=tier)
                    row = self.cloud.get(name)
                    self.assertEqual(
                        (row["kind"], row["owner_env_id"], row["owner_step"], row["parent"]),
                        (models.resource_kind(step, tier), env_id, step,
                         models.resource_parent(step, tier, REGION)))
                    self.delete(step, env_id=env_id)
                    self.assertIsNone(self.cloud.get(name))
        log = self.cloud.log()
        self.assertEqual(len(log), 2 * len(models.TIERS) * len(models.STEPS))
        self.assertEqual({r["response"] for r in log}, {"ok"})
        self.assertEqual(self.cloud.inventory(), [])

    def test_log_rows_describe_each_change(self):
        name = self.create("database")
        self.delete("database")
        self.assertEqual([(r["op"], r["name"], r["kind"], r["owner_env_id"], r["response"])
                          for r in self.cloud.log()],
                         [("create", name, "logical_db", ENV, "ok"),
                          ("delete", name, "logical_db", ENV, "ok")])

    def test_invalid_kind_or_parent_is_rejected_without_change(self):
        name = models.resource_name(ENV, "database")
        with self.assertRaises(ValueError):
            self.cloud.create(name, "bucket", ENV, "database", None, 1)
        with self.assertRaises(ValueError):
            self.cloud.create(name, "logical_db", ENV, "database", None, 1)
        with self.assertRaises(ValueError):
            self.cloud.create(name, "dedicated_db", ENV, "database", "shared-cluster-x", 1)
        self.assertEqual(self.state(), ([], []))

    def test_existing_name_is_reported_and_left_untouched(self):
        self.create()
        before = self.state()
        with self.assertRaises(AlreadyExists):
            self.create(attempt=2)
        self.assertEqual(self.state(), before)

    def test_deleting_a_missing_resource_changes_nothing(self):
        with self.assertRaises(NotFound):
            self.delete()
        self.assertEqual(self.state(), ([], []))


class FaultTest(CloudTestCase):
    def test_timeout_creates_nothing(self):
        self.with_faults(fault("timeout"))
        name = models.resource_name(ENV, "namespace")
        with self.assertRaises(CloudTimeout):
            self.create()
        self.assertEqual(self.cloud.observe(name, ENV), models.ABSENT)
        self.assertEqual(self.state(), ([], []))

    def test_timeout_after_success_creates_and_logs_the_timeout(self):
        self.with_faults(fault("timeout_after_success"))
        name = models.resource_name(ENV, "namespace")
        with self.assertRaises(CloudTimeout):
            self.create()
        self.assertEqual(self.cloud.observe(name, ENV), models.PRESENT)
        [entry] = self.cloud.log()
        self.assertEqual((entry["op"], entry["response"]), ("create", "timeout"))

    def test_quota_creates_nothing(self):
        self.with_faults(fault("quota", step="database"))
        with self.assertRaises(QuotaExceeded) as ctx:
            self.create("database")
        self.assertEqual(ctx.exception.error_class, "quota")
        self.assertEqual(self.state(), ([], []))

    def test_duplicate_create_response_is_false_and_changes_nothing(self):
        self.with_faults(fault("duplicate"))
        name = models.resource_name(ENV, "namespace")
        with self.assertRaises(AlreadyExists):
            self.create()
        self.assertEqual(self.cloud.observe(name, ENV), models.ABSENT)
        self.assertEqual(self.state(), ([], []))

    def test_delete_faults(self):
        name = self.create()
        self.with_faults(fault("timeout", op="delete"),
                         fault("duplicate", op="delete", attempt=2),
                         fault("timeout_after_success", op="delete", attempt=3))
        with self.assertRaises(CloudTimeout):
            self.delete(attempt=1)
        with self.assertRaises(NotFound):
            self.delete(attempt=2)
        self.assertEqual(self.cloud.observe(name, ENV), models.PRESENT)
        self.assertEqual(len(self.cloud.log()), 1)

        with self.assertRaises(CloudTimeout):
            self.delete(attempt=3)
        self.assertEqual(self.cloud.observe(name, ENV), models.ABSENT)
        self.assertEqual([(r["op"], r["response"]) for r in self.cloud.log()],
                         [("create", "ok"), ("delete", "timeout")])

    def test_fault_applies_only_to_its_attempt(self):
        self.with_faults(fault("quota"))
        with self.assertRaises(QuotaExceeded):
            self.create(attempt=1)
        self.create(attempt=2)
        self.assertEqual(self.cloud.observe(models.resource_name(ENV, "namespace"), ENV),
                         models.PRESENT)

    def test_resending_the_same_attempt_hits_the_same_fault(self):
        self.with_faults(fault("quota"))
        for _ in range(3):
            with self.assertRaises(QuotaExceeded):
                self.create(attempt=1)
        self.assertEqual(self.state(), ([], []))

    def test_fault_for_another_environment_does_not_apply(self):
        self.with_faults(fault("quota", request_id="req-2"))
        self.create()
        with self.assertRaises(QuotaExceeded):
            self.create(env_id=OTHER_ENV)


class FaultParsingTest(unittest.TestCase):
    def test_keys_use_the_deterministic_env_id(self):
        self.assertEqual(parse_faults([fault("quota", step="database")]),
                         {(ENV, "database", "create", 1): "quota"})

    def test_invalid_records_are_rejected(self):
        bad = {
            "unknown kind": fault("explode"),
            "quota on delete": fault("quota", op="delete"),
            "unknown step": fault("timeout", step="cache"),
            "unknown op": fault("timeout", op="update"),
            "attempt zero": fault("timeout", attempt=0),
            "attempt bool": fault("timeout", attempt=True),
            "missing field": {"request_id": REQUEST_ID, "step": "dns"},
            "not an object": ["req-1"],
        }
        for label, record in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    parse_faults([record])

    def test_two_faults_for_one_attempt_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_faults([fault("timeout"), fault("quota")])

    def test_load_faults_reads_jsonl_and_skips_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "faults.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps(fault("timeout")) + "\n\n")
                f.write(json.dumps(fault("quota", step="dns")) + "\n")
            self.assertEqual(load_faults(path), {(ENV, "namespace", "create", 1): "timeout",
                                                 (ENV, "dns", "create", 1): "quota"})


class OwnershipTest(CloudTestCase):
    def test_delete_by_another_environment_is_refused(self):
        name = self.create()
        before = self.state()
        with self.assertRaises(OwnershipMismatch):
            self.cloud.delete(name, OTHER_ENV, "namespace", 1)
        self.assertEqual(self.state(), before)

    def test_delete_with_wrong_step_is_refused(self):
        name = self.create()
        before = self.state()
        with self.assertRaises(OwnershipMismatch):
            self.cloud.delete(name, ENV, "dns", 1)
        self.assertEqual(self.state(), before)

    def test_timeout_after_success_never_deletes_a_foreign_resource(self):
        name = self.create()
        self.cloud.faults = {(OTHER_ENV, "namespace", "delete", 1): "timeout_after_success"}
        with self.assertRaises(OwnershipMismatch):
            self.cloud.delete(name, OTHER_ENV, "namespace", 1)
        self.assertEqual(self.cloud.observe(name, ENV), models.PRESENT)

    def test_observe_reports_foreign_owner(self):
        name = self.create()
        self.assertEqual(self.cloud.observe(name, ENV), models.PRESENT)
        self.assertEqual(self.cloud.observe(name, OTHER_ENV), models.FOREIGN_OWNER)
        self.assertEqual(self.cloud.observe("missing", ENV), models.ABSENT)

    def test_create_on_a_foreign_name_keeps_the_original_owner(self):
        name = self.create()
        with self.assertRaises(AlreadyExists):
            self.cloud.create(name, "namespace", OTHER_ENV, "namespace", None, 1)
        self.assertEqual(self.cloud.get(name)["owner_env_id"], ENV)


class AtomicityTest(CloudTestCase):
    def test_failed_log_insert_rolls_back_the_create(self):
        before = self.state()
        with mock.patch.object(self.cloud, "_log", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(self.state(), before)

    def test_failed_log_insert_rolls_back_the_delete(self):
        self.create()
        before = self.state()
        with mock.patch.object(self.cloud, "_log", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.delete()
        self.assertEqual(self.state(), before)

    def test_each_change_is_one_write_and_failed_calls_are_none(self):
        self.with_faults(fault("timeout", step="dns"), fault("quota", step="app"),
                         fault("duplicate", step="secret"))
        start = writes.committed_count()
        self.create("namespace")
        self.assertEqual(writes.committed_count(), start + 1)
        for step, error in (("dns", CloudTimeout), ("app", QuotaExceeded),
                            ("secret", AlreadyExists)):
            with self.assertRaises(error):
                self.create(step)
        with self.assertRaises(NotFound):
            self.delete("dns")
        self.assertEqual(writes.committed_count(), start + 1)
        self.delete("namespace")
        self.assertEqual(writes.committed_count(), start + 2)


if __name__ == "__main__":
    unittest.main()
