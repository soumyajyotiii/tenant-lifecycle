"""The independent checker: clean runs pass, deliberately corrupted evidence fails."""

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from checker import check

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def req(request_id, tier="basic", region="eu-west-1"):
    return {"request_id": request_id, "tenant": "acme", "tier": tier, "region": region}


def fault(request_id, step, kind, attempt=1, op="create"):
    return {"request_id": request_id, "step": step, "op": op, "attempt": attempt, "kind": kind}


REQUESTS = [req("r-ok"), req("r-retry", "enterprise", "us-east-1"), req("r-tas"), req("r-ok"),
            req("r-quota", "enterprise"), req("r-exhaust"), req("r-quota", "enterprise", "us-east-1")]
FAULTS = [
    fault("r-retry", "secret", "timeout"),
    fault("r-tas", "database", "timeout_after_success"),
    fault("r-quota", "dns", "quota"),
    fault("r-quota", "secret", "timeout", op="delete"),
    fault("r-quota", "secret", "duplicate", attempt=2, op="delete"),
    fault("r-exhaust", "app", "timeout"),
    fault("r-exhaust", "app", "timeout", attempt=2),
    fault("r-exhaust", "app", "duplicate", attempt=3),
]


def run(*args, crash_after=None):
    env = {k: v for k, v in os.environ.items() if k != "CRASH_AFTER"}
    if crash_after is not None:
        env["CRASH_AFTER"] = str(crash_after)
    return subprocess.run([sys.executable, "-m", *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


def write_jsonl(path, records):
    with open(path, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def produce(tmp, requests, faults, name="state"):
    """Run the real commands; return (dump rows, report rows, file paths)."""
    paths = {"requests": os.path.join(tmp, f"{name}-requests.jsonl"),
             "faults": os.path.join(tmp, f"{name}-faults.jsonl"),
             "state": os.path.join(tmp, name)}
    write_jsonl(paths["requests"], requests)
    write_jsonl(paths["faults"], faults)
    cli = ["tenant_lifecycle", "--state-dir", paths["state"]]
    assert run(*cli, "provision", paths["requests"], paths["faults"]).returncode == 0
    assert run(*cli, "agent").returncode == 0
    dump = run(*cli, "dump")
    assert dump.returncode == 0
    paths["dump"] = os.path.join(tmp, f"{name}-dump.jsonl")
    with open(paths["dump"], "w") as f:
        f.write(dump.stdout)
    paths["report"] = os.path.join(paths["state"], "agent_report.jsonl")
    return check.read_jsonl(paths["dump"]), check.read_jsonl(paths["report"]), paths


class CheckerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dump, cls.report, cls.paths = produce(cls._tmp.name, REQUESTS, FAULTS)
        cls.requests = check.read_requests(cls.paths["requests"])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def violations(self, dump=None, report=None, faults=FAULTS):
        return check.check(self.dump if dump is None else dump,
                           self.report if report is None else report, self.requests, faults)

    def codes(self, **kwargs):
        return {v["check"] for v in self.violations(**kwargs)}

    # Helpers to find rows in a copy of the dump.

    def copy(self):
        return copy.deepcopy(self.dump), copy.deepcopy(self.report)

    @staticmethod
    def rows(dump, table, **match):
        return [r for r in dump if r["table"] == table
                and all(r.get(k) == v for k, v in match.items())]

    def env_id(self, dump, request_id):
        return self.rows(dump, "environments", request_id=request_id)[0]["env_id"]

    def approved(self, dump, action):
        return next(d for d in self.rows(dump, "policy_decisions", action=action) if d["allowed"])


class CleanRunTest(CheckerTestCase):
    def test_clean_run_has_no_violations(self):
        self.assertEqual(self.violations(), [])

    def test_clean_run_passes_through_the_command_line(self):
        result = run("checker.check", "--dump", self.paths["dump"], "--report",
                     self.paths["report"], "--requests", self.paths["requests"],
                     "--faults", self.paths["faults"])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('"violations": 0', result.stderr)

    def test_checker_imports_nothing_from_the_implementation(self):
        for name in ("check.py", "run_scenarios.py"):
            with open(os.path.join(REPO_ROOT, "checker", name)) as f:
                tree = ast.parse(f.read())
            modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                       for a in n.names}
            modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            with self.subTest(file=name):
                self.assertFalse({m for m in modules if m and m.split(".")[0] in
                                  ("tenant_lifecycle", "fixtures")}, modules)


class StateAndOwnershipCorruptionTest(CheckerTestCase):
    def test_non_final_state(self):
        dump, _ = self.copy()
        self.rows(dump, "environments", request_id="r-quota")[0]["state"] = "cleanup_failed"
        self.assertTrue({"F1_not_final", "A4_history_disagrees_with_state"} <= self.codes(dump=dump))

    def test_missing_resource_of_an_active_environment(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-ok")
        dump.remove(self.rows(dump, "inventory", owner_env_id=env_id)[0])
        self.assertTrue({"F2_active_resources_wrong", "R2_claimed_but_missing",
                         "L2_inventory_not_explained_by_log"} <= self.codes(dump=dump))

    def test_resource_left_behind_by_an_escalated_environment(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-quota")
        dump.append({"table": "inventory", "name": f"{env_id}-namespace", "kind": "namespace",
                     "owner_env_id": env_id, "owner_step": "namespace", "parent": None})
        dump.sort(key=lambda r: (check.TABLE_ORDER.index(r["table"]),
                                 tuple(r[k] for k in check.TABLE_KEYS[r["table"]])))
        self.assertIn("F3_resources_left_behind", self.codes(dump=dump))

    def test_orphan_resource_with_no_registry_account(self):
        dump, _ = self.copy()
        dump.append({"table": "inventory", "name": "zz-orphan", "kind": "dns",
                     "owner_env_id": "env-unknown", "owner_step": "dns", "parent": None})
        codes = self.codes(dump=dump)
        self.assertTrue({"R1_unaccounted_resource", "L2_inventory_not_explained_by_log"} <= codes)

    def test_wrong_kind_on_an_active_resource(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-ok")
        self.rows(dump, "inventory", owner_env_id=env_id, owner_step="database")[0]["kind"] = \
            "dedicated_db"
        self.assertTrue({"F2_active_resources_wrong", "R1_account_mismatch"} <= self.codes(dump=dump))

    def test_open_attempt(self):
        dump, _ = self.copy()
        self.rows(dump, "step_attempts")[0]["outcome"] = None
        self.assertTrue({"F4_open_attempt", "A3_outcome_count"} <= self.codes(dump=dump))

    def test_delete_missing_from_the_cloud_log(self):
        dump, _ = self.copy()
        dump.remove(self.rows(dump, "cloud_log", op="delete")[0])
        self.assertTrue({"L2_inventory_not_explained_by_log",
                         "L3_delete_outcome_without_log"} <= self.codes(dump=dump))


class DuplicateCorruptionTest(CheckerTestCase):
    def test_second_environment_for_one_request(self):
        dump, _ = self.copy()
        clone = dict(self.rows(dump, "environments", request_id="r-ok")[0], env_id="env-zzzz")
        dump.insert(dump.index(self.rows(dump, "environments")[-1]) + 1, clone)
        self.assertIn("D1_duplicate_environment", self.codes(dump=dump))

    def test_missing_duplicate_audit(self):
        dump, _ = self.copy()
        dump.remove(self.rows(dump, "audit", kind="duplicate_ignored")[0])
        self.assertIn("D3_duplicate_audit_wrong", self.codes(dump=dump))

    def test_wrong_same_payload_flag(self):
        dump, _ = self.copy()
        event = self.rows(dump, "audit", kind="duplicate_ignored", ref="line:7")[0]
        event["details"]["same_payload"] = True
        self.assertIn("D3_same_payload_wrong", self.codes(dump=dump))


class AuditCorruptionTest(CheckerTestCase):
    def test_missing_outcome_record(self):
        dump, _ = self.copy()
        dump.remove(self.rows(dump, "audit", kind="op_outcome")[0])
        self.assertTrue({"A3_outcome_count", "A1_sequence_gap"} <= self.codes(dump=dump))

    def test_outcome_recorded_before_intent(self):
        dump, _ = self.copy()
        intent = self.rows(dump, "audit", kind="op_intent")[0]
        outcome = self.rows(dump, "audit", kind="op_outcome", env_id=intent["env_id"],
                            ref=intent["ref"])[0]
        intent["seq"], outcome["seq"] = outcome["seq"], intent["seq"]
        self.assertIn("A3_outcome_before_intent", self.codes(dump=dump))

    def test_outcome_record_disagrees_with_attempt(self):
        dump, _ = self.copy()
        self.rows(dump, "audit", kind="op_outcome")[0]["details"]["outcome"] = "absent"
        self.assertIn("A3_outcome_disagrees", self.codes(dump=dump))

    def test_illegal_transition(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-ok")
        change = self.rows(dump, "audit", kind="state_changed", env_id=env_id)[0]
        change["details"]["to"] = "active"
        change["ref"] = "pending->active"
        change["details"]["from"] = "pending"
        self.assertIn("A4_illegal_transition", self.codes(dump=dump))

    def test_attempt_beyond_the_retry_budget(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-exhaust")
        attempt = self.rows(dump, "step_attempts", env_id=env_id, step="app", attempt=3)[0]
        attempt["attempt"] = 4
        for e in self.rows(dump, "audit", env_id=env_id, ref="app/create/3"):
            e["ref"] = "app/create/4"
        self.assertTrue({"A5_budget_exceeded", "A5_attempts_not_sequential"} <=
                        self.codes(dump=dump))

    def test_unsorted_dump(self):
        dump, _ = self.copy()
        dump[0], dump[1] = dump[1], dump[0]
        self.assertIn("C3_not_sorted", self.codes(dump=dump))


class ApprovalCorruptionTest(CheckerTestCase):
    def test_agent_action_without_its_approval(self):
        dump, _ = self.copy()
        decision = self.approved(dump, "retry_step")
        dump.remove(decision)
        dump.remove(self.rows(dump, "audit", kind="policy_decision",
                              ref=decision["decision_id"])[0])
        codes = self.codes(dump=dump)
        self.assertTrue({"P2_agent_change_without_approval",
                         "P3_completion_without_approval"} <= codes)

    def test_denied_decision_that_was_executed_anyway(self):
        dump, _ = self.copy()
        decision = self.approved(dump, "escalate")
        decision["allowed"] = False
        self.rows(dump, "audit", kind="policy_decision",
                  ref=decision["decision_id"])[0]["details"]["allowed"] = False
        self.assertIn("P2_agent_change_without_approval", self.codes(dump=dump))

    def test_approval_the_rules_do_not_justify(self):
        # Make the retried attempt look like a quota error: retrying it was never allowed.
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-retry")
        self.rows(dump, "step_attempts", env_id=env_id, step="secret",
                  attempt=1)[0]["error_class"] = "quota"
        self.rows(dump, "audit", env_id=env_id, kind="op_outcome",
                  ref="secret/create/1")[0]["details"]["error_class"] = "quota"
        self.assertIn("P4_approval_not_justified", self.codes(dump=dump))

    def test_escalation_while_a_retry_was_still_possible(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-quota")
        for row in (self.rows(dump, "step_attempts", env_id=env_id, step="dns")[0],
                    self.rows(dump, "audit", env_id=env_id, kind="op_outcome",
                              ref="dns/create/1")[0]["details"]):
            row["error_class"] = "timeout"
        self.assertIn("P4_approval_not_justified", self.codes(dump=dump))

    def test_action_outside_the_allowlist(self):
        dump, report = self.copy()
        decision = self.approved(dump, "resume")
        decision["action"] = "delete_tenant"
        self.rows(dump, "audit", kind="policy_decision",
                  ref=decision["decision_id"])[0]["details"]["action"] = "delete_tenant"
        self.assertIn("P1_action_not_allowlisted", self.codes(dump=dump))

    def test_workflow_acting_on_an_agent_owned_environment(self):
        dump, _ = self.copy()
        env_id = self.env_id(dump, "r-retry")
        event = [e for e in self.rows(dump, "audit", env_id=env_id, actor="agent")
                 if e["kind"] == "op_intent"][0]
        event["actor"] = "workflow"
        self.assertIn("P2_workflow_change_in_agent_scope", self.codes(dump=dump))

    def test_change_outside_the_approved_scope(self):
        dump, _ = self.copy()
        decision = self.approved(dump, "cleanup_orphan")
        decision["target"] = "namespace/2"
        self.assertIn("P2_change_outside_approved_scope", self.codes(dump=dump))


class ReportCorruptionTest(CheckerTestCase):
    def test_missing_report_row(self):
        _, report = self.copy()
        report.pop()
        self.assertIn("Q1_report_does_not_match_decisions", self.codes(report=report))

    def test_altered_evidence(self):
        _, report = self.copy()
        next(r for r in report if r["action"] == "retry_step")["evidence"]["error_class"] = "quota"
        self.assertIn("Q2_evidence_disagrees", self.codes(report=report))

    def test_report_claims_cleanup_while_resources_remain(self):
        _, report = self.copy()
        row = next(r for r in report if r["action"] == "escalate" and r["executed"]
                   and r["outcome"]["env_state"] == "cleanup_failed")
        row["outcome"]["env_state"] = "escalated"
        codes = self.codes(report=report)
        self.assertTrue({"Q4_report_claims_cleanup_with_resources",
                         "Q4_outcome_not_supported_by_evidence"} <= codes)

    def test_report_outcome_hides_a_failed_attempt(self):
        _, report = self.copy()
        row = next(r for r in report if r["action"] == "cleanup_orphan" and r["executed"])
        row["outcome"]["attempts"] = []
        self.assertTrue({"Q3_outcome_disagrees_with_audit",
                         "Q4_outcome_not_supported_by_evidence"} <= self.codes(report=report))

    def test_report_marks_a_denied_action_as_executed(self):
        _, report = self.copy()
        next(r for r in report if not r["allowed"])["executed"] = True
        self.assertIn("Q3_executed_flag_wrong", self.codes(report=report))

    def test_report_from_another_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, other_report, _ = produce(tmp, REQUESTS, FAULTS[:2], name="other")
        self.assertIn("Q1_report_does_not_match_decisions", self.codes(report=other_report))


class FaultEvidenceTest(CheckerTestCase):
    def test_fault_that_never_happened(self):
        extra = FAULTS + [fault("r-ok", "app", "timeout", attempt=2)]
        self.assertIn("K1_fault_not_exercised", self.codes(faults=extra))

    def test_quota_that_did_not_escalate(self):
        extra = FAULTS + [fault("r-ok", "dns", "quota", attempt=9)]
        self.assertIn("K2_quota_not_escalated", self.codes(faults=extra))


class RealViolationTest(unittest.TestCase):
    """A real run that cannot meet semantic 1 must be reported, not hidden."""

    def test_exhausted_cleanup_budget_is_reported(self):
        faults = [fault("r1", "database", "quota")] + [
            fault("r1", "namespace", "timeout", attempt=n, op="delete") for n in (1, 2, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            dump, report, paths = produce(tmp, [req("r1")], faults)
            result = run("checker.check", "--dump", paths["dump"], "--report", paths["report"])
        codes = {v["check"] for v in check.check(dump, report)}
        self.assertEqual(codes, {"F1_not_final"})
        self.assertEqual(result.returncode, 1)


class CommandLineTest(CheckerTestCase):
    def test_corrupted_files_exit_1_and_unreadable_files_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump, _ = self.copy()
            dump.pop()
            bad = os.path.join(tmp, "bad-dump.jsonl")
            write_jsonl(bad, dump)
            result = run("checker.check", "--dump", bad, "--report", self.paths["report"])
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.stdout.strip())

            broken = os.path.join(tmp, "broken.jsonl")
            with open(broken, "w") as f:
                f.write("{not json\n")
            result = run("checker.check", "--dump", broken, "--report", self.paths["report"])
            self.assertEqual(result.returncode, 2)


class RunnerTest(unittest.TestCase):
    def test_runner_passes_on_a_small_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            requests, faults = os.path.join(tmp, "r.jsonl"), os.path.join(tmp, "f.jsonl")
            write_jsonl(requests, REQUESTS)
            write_jsonl(faults, FAULTS)
            result = run("checker.run_scenarios", "--requests", requests, "--faults", faults,
                         "--crash-samples", "3", "--workdir", os.path.join(tmp, "work"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["ok"])
        self.assertTrue(any(p.get("cloud_window") for p in summary["crash_points"]))
        self.assertTrue(all(p["identical"] for p in summary["crash_points"]))

    def test_runner_fails_when_the_scenario_breaks_an_invariant(self):
        faults = [fault("r1", "database", "quota")] + [
            fault("r1", "namespace", "timeout", attempt=n, op="delete") for n in (1, 2, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            requests, fpath = os.path.join(tmp, "r.jsonl"), os.path.join(tmp, "f.jsonl")
            write_jsonl(requests, [req("r1"), req("r2")])
            write_jsonl(fpath, faults)
            result = run("checker.run_scenarios", "--requests", requests, "--faults", fpath,
                         "--crash-samples", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("F1_not_final", result.stdout)


if __name__ == "__main__":
    unittest.main()
