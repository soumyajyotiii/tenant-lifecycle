"""The agent recovering what a crashed provision left behind, through the real CLI."""

import copy
import json
import os
import shutil
import tempfile
import unittest

from checker import check

from test_checker import fault, req, run, write_jsonl

REQUESTS = [req("r1"), req("r2", "enterprise", "us-east-1"), req("r1"), req("r3")]
FAULTS = [
    fault("r1", "database", "timeout_after_success"),
    fault("r2", "secret", "quota"),
    fault("r3", "dns", "timeout"),
]


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = self.path("requests.jsonl")
        self.faults = self.path("faults.jsonl")
        write_jsonl(self.requests, REQUESTS)
        write_jsonl(self.faults, FAULTS)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def cli(self, state, *args, crash_after=None, expect=0):
        result = run("tenant_lifecycle", "--state-dir", state, *args, crash_after=crash_after)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def provision(self, state, crash_after=None, expect=0):
        return self.cli(state, "provision", self.requests, self.faults,
                        crash_after=crash_after, expect=expect)

    def rows(self, state):
        return [json.loads(line) for line in self.cli(state, "dump").stdout.splitlines()]

    def report(self, state):
        return check.read_jsonl(os.path.join(state, "agent_report.jsonl"))

    def crash_where(self, predicate, limit=200):
        """Crash provision at the first write count whose state satisfies predicate."""
        for n in range(1, limit):
            state = self.path(f"crash-{n}")
            if self.provision(state, crash_after=n, expect=None).returncode != 3:
                break
            rows = self.rows(state)
            if predicate(rows):
                return state, rows
            shutil.rmtree(state)
        self.fail("no crash point matched")

    def finish_with_agent_first(self, state):
        """Agent recovers what it can, provision finishes the file, agent resolves the rest."""
        self.cli(state, "agent")
        self.provision(state)
        self.cli(state, "agent")
        rows = self.rows(state)
        violations = check.check(rows, self.report(state), check.read_requests(self.requests),
                                 check.read_jsonl(self.faults))
        self.assertEqual(violations, [])
        return rows


def open_creates(rows):
    return [r for r in rows if r["table"] == "step_attempts" and r["outcome"] is None]


def inventory_names(rows):
    return {r["name"] for r in rows if r["table"] == "inventory"}


def env(rows, request_id):
    return next(r for r in rows if r["table"] == "environments" and r["request_id"] == request_id)


class AgentRecoversProvisionCrashTest(RecoveryTestCase):
    def test_open_attempt_with_resource_already_created_is_recorded_without_a_new_call(self):
        def window(rows):
            return any(f"{a['env_id']}-{a['step']}" in inventory_names(rows)
                       for a in open_creates(rows))
        state, crashed = self.crash_where(window)
        [attempt] = open_creates(crashed)
        name = f"{attempt['env_id']}-{attempt['step']}"

        result = self.cli(state, "agent")
        rows = self.rows(state)
        self.assertEqual(open_creates(rows), [])
        recovered = next(r for r in rows if r["table"] == "environments"
                         and r["env_id"] == attempt["env_id"])
        self.assertNotIn(recovered["state"], ("pending", "provisioning"))
        creates = [c for c in rows if c["table"] == "cloud_log" and c["name"] == name
                   and c["op"] == "create"]
        self.assertEqual(len(creates), 1)
        closed = [e for e in rows if e["table"] == "audit" and e["kind"] == "op_outcome"
                  and e["env_id"] == attempt["env_id"]
                  and e["ref"] == f"{attempt['step']}/create/{attempt['attempt']}"]
        self.assertEqual([(e["actor"], e["details"]["outcome"]) for e in closed],
                         [("agent", "present")])
        resume = [r for r in map(json.loads, result.stdout.splitlines())
                  if r["env_id"] == attempt["env_id"]][0]
        self.assertEqual((resume["action"], resume["allowed"], resume["evidence"]["open_attempts"]),
                         ("resume", True, 1))
        self.finish_with_agent_first(state)

    def test_open_attempt_with_nothing_created_resends_the_same_attempt(self):
        def before_call(rows):
            r2 = [r for r in rows if r["table"] == "environments" and r["request_id"] == "r2"]
            return bool(r2) and any(a["env_id"] == r2[0]["env_id"] and a["step"] == "secret"
                                    for a in open_creates(rows))
        state, _ = self.crash_where(before_call)
        self.cli(state, "agent")
        rows = self.rows(state)
        r2 = env(rows, "r2")
        secret = [a for a in rows if a["table"] == "step_attempts" and a["env_id"] == r2["env_id"]
                  and a["step"] == "secret"]
        # The same attempt 1 hit the same quota fault, so r2 escalates instead of retrying.
        self.assertEqual([(a["attempt"], a["outcome"], a["error_class"]) for a in secret],
                         [(1, "absent", "quota")])
        self.assertEqual(r2["state"], "escalated")
        self.finish_with_agent_first(state)

    def test_environment_left_pending_is_provisioned_by_the_agent(self):
        def pending(rows):
            return any(r["table"] == "environments" and r["state"] == "pending" for r in rows)
        state, crashed = self.crash_where(pending)
        stuck = next(r for r in crashed if r["table"] == "environments" and r["state"] == "pending")
        self.cli(state, "agent")
        rows = self.rows(state)
        self.assertNotIn(env(rows, stuck["request_id"])["state"], ("pending", "provisioning"))
        self.finish_with_agent_first(state)

    def test_provision_leaves_an_agent_recovered_environment_alone(self):
        state, _ = self.crash_where(lambda rows: bool(open_creates(rows)))
        self.cli(state, "agent")
        before = self.rows(state)
        self.provision(state)
        after = self.rows(state)
        recovered = {r["env_id"] for r in before if r["table"] == "policy_decisions"}
        self.assertTrue(recovered)
        for env_id in recovered:
            old = [r for r in before if r.get("env_id") == env_id]
            new = [r for r in after if r.get("env_id") == env_id]
            # Only a duplicate line the crash had not reached yet may be recorded.
            added = [r for r in new if r not in old]
            self.assertEqual([r for r in new if r in old], old)
            self.assertTrue(all(r["table"] == "audit" and r["kind"] == "duplicate_ignored"
                                for r in added), added)

    def test_agent_recovery_from_every_provision_crash_point_ends_valid(self):
        baseline = self.path("baseline")
        self.provision(baseline)
        total = sum(1 for r in self.rows(baseline) if r["table"] in ("audit", "cloud_log")) + 1
        for n in range(1, total + 1):
            with self.subTest(crash_after=n):
                state = self.path(f"sweep-{n}")
                self.provision(state, crash_after=n, expect=3)
                rows = self.finish_with_agent_first(state)
                states = {r["state"] for r in rows if r["table"] == "environments"}
                self.assertLessEqual(states, {"active", "escalated"})
                shutil.rmtree(state)


class AgentOutputTest(RecoveryTestCase):
    def test_agent_prints_the_report_and_replay_prints_the_same(self):
        state = self.path("state")
        self.provision(state)
        first = self.cli(state, "agent")
        with open(os.path.join(state, "agent_report.jsonl")) as f:
            self.assertEqual(first.stdout, f.read())
        rows = [json.loads(line) for line in first.stdout.splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all({"decision_id", "action", "evidence", "rule", "executed",
                             "outcome"} <= set(r) for r in rows))
        self.assertEqual(json.loads(first.stderr), {"active": 2, "escalated": 1})
        self.assertEqual(self.cli(state, "agent").stdout, first.stdout)


class CrossActorCheckerTest(RecoveryTestCase):
    """The checker accepts the agent closing a provision attempt only inside an approval."""

    def recovered(self):
        state, _ = self.crash_where(lambda rows: bool(open_creates(rows)))
        self.cli(state, "agent")
        return self.rows(state), self.report(state)

    def test_recovered_run_is_clean(self):
        rows, report = self.recovered()
        self.assertEqual(check.check(rows, report), [])

    def test_workflow_closing_an_agent_attempt_is_flagged(self):
        rows, report = self.recovered()
        rows = copy.deepcopy(rows)
        event = next(e for e in rows if e["table"] == "audit" and e["kind"] == "op_outcome"
                     and e["actor"] == "agent")
        attempt = next(a for a in rows if a["table"] == "step_attempts"
                       and a["env_id"] == event["env_id"]
                       and f"{a['step']}/{a['op']}/{a['attempt']}" == event["ref"])
        if attempt["actor"] == "workflow":
            attempt["actor"] = "agent"
            intent = next(e for e in rows if e["table"] == "audit" and e["kind"] == "op_intent"
                          and e["env_id"] == event["env_id"] and e["ref"] == event["ref"])
            intent["actor"] = "agent"
        event["actor"] = "workflow"
        codes = {v["check"] for v in check.check(rows, report)}
        self.assertIn("A3_actor_disagrees", codes)

    def test_agent_closing_a_provision_attempt_without_approval_is_flagged(self):
        rows, report = self.recovered()
        rows = copy.deepcopy(rows)
        closing = next(e for e in rows if e["table"] == "audit" and e["kind"] == "op_outcome"
                       and e["actor"] == "agent")
        resume = next(d for d in rows if d["table"] == "policy_decisions"
                      and d["env_id"] == closing["env_id"] and d["action"] == "resume")
        resume["allowed"] = False
        next(e for e in rows if e["table"] == "audit" and e["kind"] == "policy_decision"
             and e["ref"] == resume["decision_id"])["details"]["allowed"] = False
        codes = {v["check"] for v in check.check(rows, report)}
        self.assertIn("P2_agent_change_without_approval", codes)


if __name__ == "__main__":
    unittest.main()
