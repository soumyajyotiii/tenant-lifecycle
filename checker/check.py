"""Independent checker for an exported dump and agent report.

    python3 -m checker.check --dump dump.jsonl --report agent_report.jsonl \
        [--requests requests.jsonl] [--faults faults.jsonl]

It imports nothing from tenant_lifecycle. The rules below are restated from
the specification and the approved decisions, and every claim in the dump or
report is re-derived from raw evidence (audit order, cloud log, inputs) rather
than taken from the implementation's own verdicts.

Exit codes: 0 no violations, 1 violations found, 2 unreadable input.
"""

import argparse
import collections
import json
import sys

STEPS = ("namespace", "database", "secret", "dns", "app")
FINAL_STATES = {"active", "escalated", "cleaned_up"}
EMPTY_FINAL_STATES = {"escalated", "cleaned_up"}
TRANSITIONS = {
    ("pending", "provisioning"), ("provisioning", "active"), ("provisioning", "step_failed"),
    ("step_failed", "provisioning"), ("step_failed", "compensating"),
    ("compensating", "escalated"), ("compensating", "cleaned_up"),
    ("compensating", "cleanup_failed"), ("cleanup_failed", "compensating"),
}
ALLOWED_ACTIONS = {"retry_step", "resume", "cleanup_orphan", "escalate"}
MAX_CREATE_ATTEMPTS = 3
MAX_DELETE_ATTEMPTS = 3
RETRYABLE_ERRORS = {"timeout", "already_exists"}
OWNED_OUTCOMES = {"present", "mismatch"}  # our owner is on the resource
TABLE_KEYS = {
    "environments": ("env_id",), "step_attempts": ("env_id", "step", "op", "attempt"),
    "resources": ("name",), "policy_decisions": ("decision_id",), "audit": ("env_id", "seq"),
    "inventory": ("name",), "cloud_log": ("seq",),
}
TABLE_ORDER = tuple(TABLE_KEYS)
EXPECTED_FAULT_OUTCOME = {
    ("create", "timeout"): ("absent", "timeout"),
    ("create", "duplicate"): ("absent", "already_exists"),
    ("create", "quota"): ("absent", "quota"),
    ("create", "timeout_after_success"): ("present", None),
    ("delete", "timeout"): ("present", "timeout"),
    ("delete", "duplicate"): ("present", "not_found"),
    ("delete", "timeout_after_success"): ("absent", None),
}


def expected_resource(env, step):
    """(kind, parent) a correctly provisioned resource must have for this env and step."""
    if step == "database":
        if env["tier"] == "basic":
            return "logical_db", f"shared-cluster-{env['region']}"
        return "dedicated_db", None
    return step, None


class Findings:
    def __init__(self):
        self.items = []

    def add(self, check, detail):
        self.items.append({"check": check, "detail": detail})


def check(dump_rows, report_rows, requests=None, faults=None):
    """Return a list of violations, each {"check": code, "detail": text}."""
    f = Findings()
    _check_structure(f, dump_rows)
    data = _index(dump_rows)
    _check_final_states(f, data)
    _check_accounting(f, data)
    _check_duplicates(f, data, requests)
    _check_audit(f, data)
    _check_cloud_log(f, data)
    windows = _check_approvals(f, data)
    _check_report(f, data, report_rows, windows)
    if faults is not None:
        _check_faults(f, data, faults)
    return f.items


def _index(rows):
    tables = collections.defaultdict(list)
    for row in rows:
        tables[row.get("table")].append(row)
    audit = collections.defaultdict(list)
    for e in tables["audit"]:
        audit[e["env_id"]].append(e)
    for events in audit.values():
        events.sort(key=lambda e: e["seq"])
    return {
        "envs": {e["env_id"]: e for e in tables["environments"]},
        "attempts": tables["step_attempts"],
        "resources": {r["name"]: r for r in tables["resources"]},
        "decisions": {d["decision_id"]: d for d in tables["policy_decisions"]},
        "audit": audit,
        "inventory": {i["name"]: i for i in tables["inventory"]},
        "cloud_log": sorted(tables["cloud_log"], key=lambda c: c["seq"]),
    }


# Structure and determinism

def _check_structure(f, rows):
    keys = []
    for n, row in enumerate(rows, start=1):
        table = row.get("table")
        if table not in TABLE_KEYS:
            f.add("C1_unknown_table", f"line {n}: {table!r}")
            continue
        try:
            keys.append((TABLE_ORDER.index(table), tuple(row[k] for k in TABLE_KEYS[table])))
        except KeyError as e:
            f.add("C1_missing_key", f"line {n}: {table} lacks {e}")
    if len(set(keys)) != len(keys):
        f.add("C2_duplicate_primary_key", f"{len(keys) - len(set(keys))} repeated rows")
    if keys != sorted(keys):
        f.add("C3_not_sorted", "dump rows are not in table and primary-key order")


# Semantic 1: final states

def _check_final_states(f, d):
    owned = collections.defaultdict(dict)
    for item in d["inventory"].values():
        owned[item["owner_env_id"]][item["name"]] = item
    for env_id, env in d["envs"].items():
        if env["state"] not in FINAL_STATES:
            f.add("F1_not_final", f"{env['request_id']} ended {env['state']}")
        mine = owned[env_id]
        if env["state"] == "active":
            want = {f"{env_id}-{s}": (s, *expected_resource(env, s)) for s in STEPS}
            have = {n: (i["owner_step"], i["kind"], i["parent"]) for n, i in mine.items()}
            if have != want:
                f.add("F2_active_resources_wrong", f"{env['request_id']}: {sorted(have)}")
        elif env["state"] in EMPTY_FINAL_STATES and mine:
            f.add("F3_resources_left_behind", f"{env['request_id']} {env['state']} still owns "
                                              f"{sorted(mine)}")
    for a in d["attempts"]:
        if a["outcome"] is None:
            f.add("F4_open_attempt", f"{a['env_id']} {a['step']}/{a['op']}/{a['attempt']}")


# Semantic 2: every cloud resource is accounted for

def _check_accounting(f, d):
    for name, item in d["inventory"].items():
        account = d["resources"].get(name)
        if account is None:
            f.add("R1_unaccounted_resource", name)
            continue
        if (account["env_id"], account["step"], account["kind"], account["parent"]) != \
                (item["owner_env_id"], item["owner_step"], item["kind"], item["parent"]):
            f.add("R1_account_mismatch", name)
    for name, account in d["resources"].items():
        if account["status"] in OWNED_OUTCOMES and name not in d["inventory"]:
            f.add("R2_claimed_but_missing", f"{name} is {account['status']} in the registry")
        if account["status"] not in OWNED_OUTCOMES and name in d["inventory"] and \
                d["inventory"][name]["owner_env_id"] == account["env_id"]:
            f.add("R2_present_but_not_claimed", f"{name} is {account['status']} in the registry")


# Semantic 3: duplicates

def _check_duplicates(f, d, requests):
    by_request = collections.Counter(e["request_id"] for e in d["envs"].values())
    for rid, n in by_request.items():
        if n > 1:
            f.add("D1_duplicate_environment", f"{rid} has {n} environments")
    if requests is None:
        return
    first, later = {}, collections.defaultdict(list)
    for line_no, payload in requests:
        rid = payload.get("request_id")
        if rid in first:
            later[rid].append((line_no, payload))
        else:
            first[rid] = (line_no, payload)
    env_by_request = {e["request_id"]: e for e in d["envs"].values()}
    if set(env_by_request) != set(first):
        f.add("D2_environment_set_wrong",
              f"missing {sorted(set(first) - set(env_by_request))[:5]}, "
              f"extra {sorted(set(env_by_request) - set(first))[:5]}")
    for rid, (line_no, payload) in first.items():
        env = env_by_request.get(rid)
        if env is None:
            continue
        if env["source_line"] != line_no or any(
                env[k] != payload.get(k) for k in ("tenant", "tier", "region")):
            f.add("D2_not_first_occurrence", rid)
        dup_events = {e["ref"]: e for e in d["audit"].get(env["env_id"], [])
                      if e["kind"] == "duplicate_ignored"}
        expected_refs = {f"line:{n}" for n, _ in later.get(rid, [])}
        if set(dup_events) != expected_refs:
            f.add("D3_duplicate_audit_wrong", f"{rid}: {sorted(dup_events)} != "
                                              f"{sorted(expected_refs)}")
        for n, dup in later.get(rid, []):
            e = dup_events.get(f"line:{n}")
            if e is not None and e["details"].get("same_payload") != (dup == payload):
                f.add("D3_same_payload_wrong", f"{rid} line {n}")


# Semantic 5: audit completeness

def _ref(a):
    return f"{a['step']}/{a['op']}/{a['attempt']}"


def _check_audit(f, d):
    attempts_by_env = collections.defaultdict(dict)
    for a in d["attempts"]:
        attempts_by_env[a["env_id"]][_ref(a)] = a
    for env_id, env in d["envs"].items():
        events = d["audit"].get(env_id, [])
        if [e["seq"] for e in events] != list(range(1, len(events) + 1)):
            f.add("A1_sequence_gap", env["request_id"])
        if not events or events[0]["kind"] != "request_registered":
            f.add("A2_registration_missing", env["request_id"])
        attempts = attempts_by_env[env_id]
        intents = collections.defaultdict(list)
        outcomes = collections.defaultdict(list)
        for e in events:
            if e["kind"] == "op_intent":
                intents[e["ref"]].append(e)
            elif e["kind"] == "op_outcome":
                outcomes[e["ref"]].append(e)
        for ref in set(intents) | set(outcomes):
            if ref not in attempts:
                f.add("A3_event_without_attempt", f"{env['request_id']} {ref}")
        for ref, a in attempts.items():
            i, o = intents.get(ref, []), outcomes.get(ref, [])
            if len(i) != 1:
                f.add("A3_intent_count", f"{env['request_id']} {ref}: {len(i)}")
            if len(o) != (0 if a["outcome"] is None else 1):
                f.add("A3_outcome_count", f"{env['request_id']} {ref}: {len(o)}")
            if len(i) == 1 and len(o) == 1:
                if o[0]["seq"] < i[0]["seq"]:
                    f.add("A3_outcome_before_intent", f"{env['request_id']} {ref}")
                if (o[0]["details"].get("outcome"), o[0]["details"].get("error_class")) != \
                        (a["outcome"], a["error_class"]):
                    f.add("A3_outcome_disagrees", f"{env['request_id']} {ref}")
                # Only the agent may close an attempt someone else opened: a
                # provision attempt left open by a crash. P2 checks it was approved.
                if i[0]["actor"] != a["actor"] or o[0]["actor"] not in (
                        {a["actor"], "agent"} if a["actor"] == "workflow" else {a["actor"]}):
                    f.add("A3_actor_disagrees", f"{env['request_id']} {ref}")
        _check_state_history(f, env, events, intents, outcomes)
        _check_budgets(f, env, attempts.values())


def _check_state_history(f, env, events, intents, outcomes):
    state = "pending"
    for e in events:
        if e["kind"] != "state_changed":
            continue
        frm, to = e["details"].get("from"), e["details"].get("to")
        if frm != state or (frm, to) not in TRANSITIONS or e["ref"] != f"{frm}->{to}":
            f.add("A4_illegal_transition", f"{env['request_id']} seq {e['seq']}: {state} then "
                                           f"{frm}->{to}")
        for ref, (intent,) in ((r, i) for r, i in intents.items() if len(i) == 1):
            closed = outcomes.get(ref)
            if intent["seq"] < e["seq"] and (not closed or closed[0]["seq"] > e["seq"]):
                f.add("A4_transition_while_unresolved", f"{env['request_id']} seq {e['seq']} "
                                                        f"with {ref} open")
        state = to
    if state != env["state"]:
        f.add("A4_history_disagrees_with_state", f"{env['request_id']}: {state} != "
                                                 f"{env['state']}")


def _check_budgets(f, env, attempts):
    numbers = collections.defaultdict(list)
    for a in attempts:
        numbers[(a["step"], a["op"])].append(a["attempt"])
    for (step, op), ns in numbers.items():
        limit = MAX_CREATE_ATTEMPTS if op == "create" else MAX_DELETE_ATTEMPTS
        if sorted(ns) != list(range(1, len(ns) + 1)):
            f.add("A5_attempts_not_sequential", f"{env['request_id']} {step}/{op}: {sorted(ns)}")
        if max(ns) > limit:
            f.add("A5_budget_exceeded", f"{env['request_id']} {step}/{op}: {max(ns)} > {limit}")


# The inventory must be exactly what the cloud log says happened

def _check_cloud_log(f, d):
    replayed = {}
    for c in d["cloud_log"]:
        if c["op"] == "create":
            if c["name"] in replayed:
                f.add("L1_double_create", c["name"])
            replayed[c["name"]] = (c["kind"], c["owner_env_id"])
        elif c["op"] == "delete":
            current = replayed.pop(c["name"], None)
            if current is None or current[1] != c["owner_env_id"]:
                f.add("L1_delete_not_by_owner", c["name"])
        else:
            f.add("L1_unknown_op", f"seq {c['seq']}")
    actual = {n: (i["kind"], i["owner_env_id"]) for n, i in d["inventory"].items()}
    if replayed != actual:
        f.add("L2_inventory_not_explained_by_log",
              f"{sorted(set(replayed) ^ set(actual))[:5]}")
    creates = collections.Counter(c["name"] for c in d["cloud_log"] if c["op"] == "create")
    deletes = collections.Counter(c["name"] for c in d["cloud_log"] if c["op"] == "delete")
    for a in d["attempts"]:
        name = f"{a['env_id']}-{a['step']}"
        if a["op"] == "create" and a["outcome"] == "present" and creates[name] != 1:
            f.add("L3_create_outcome_without_log", f"{name} attempt {a['attempt']}")
        if a["op"] == "delete" and a["outcome"] == "absent" and deletes[name] != 1:
            f.add("L3_delete_outcome_without_log", f"{name} attempt {a['attempt']}")
    claimed = collections.Counter()
    for a in d["attempts"]:
        if a["outcome"] == "present" and a["op"] == "create":
            claimed[f"{a['env_id']}-{a['step']}"] += 1
    for name, n in creates.items():
        owner = name.rsplit("-", 1)[0]
        if owner in d["envs"] and claimed[name] == 0:
            f.add("L3_log_without_create_outcome", name)


# Semantic 4: policy approval before every agent action, judged independently

class _History:
    """What the audit trail had established up to a point, rebuilt event by event."""

    def __init__(self):
        self.state = "pending"
        self.last = {}           # (step, op) -> (attempt, outcome, error_class)
        self.last_any = {}       # step -> latest outcome of any op
        self.open = set()

    def apply(self, e):
        if e["kind"] == "state_changed":
            self.state = e["details"].get("to")
        elif e["kind"] == "op_intent":
            self.open.add(e["ref"])
        elif e["kind"] == "op_outcome":
            self.open.discard(e["ref"])
            step, op, attempt = e["ref"].split("/")
            outcome = e["details"].get("outcome")
            self.last[(step, op)] = (int(attempt), outcome, e["details"].get("error_class"))
            self.last_any[step] = outcome

    def owned(self, env_id):
        return sorted(f"{env_id}-{s}" for s, o in self.last_any.items() if o in OWNED_OUTCOMES)

    def failed_step(self):
        for step in STEPS:
            last = self.last.get((step, "create"))
            if last is None or last[1] != "present":
                return step, last
        return None, None


def _judge(h, action, target):
    """Would the approved rules allow this action given only the facts so far?"""
    if action == "retry_step":
        step, _, n = target.partition("/")
        failed, last = h.failed_step()
        return (h.state == "step_failed" and step == failed and last is not None
                and n.isdigit() and int(n) == last[0] + 1 <= MAX_CREATE_ATTEMPTS
                and last[1] == "absent" and last[2] in RETRYABLE_ERRORS)
    if action == "resume":
        # Unresolved creates are allowed: resume must reconcile them first.
        return (h.state in ("pending", "provisioning")
                and all(ref.split("/")[1] == "create" for ref in h.open))
    if action == "cleanup_orphan":
        step, _, n = target.partition("/")
        last = h.last.get((step, "delete"))
        return (h.state == "cleanup_failed" and last is not None and last[1] == "present"
                and h.last_any.get(step) == "present"
                and n.isdigit() and int(n) == last[0] + 1 <= MAX_DELETE_ATTEMPTS)
    if action == "escalate":
        if h.state == "step_failed":
            _, last = h.failed_step()
            return (last is None or last[1] != "absent" or last[2] not in RETRYABLE_ERRORS
                    or last[0] >= MAX_CREATE_ATTEMPTS)
        if h.state == "cleanup_failed":
            pending = any(op == "delete" and v[1] == "present" for (s, op), v in h.last.items())
            mismatched = any(o == "mismatch" for o in h.last_any.values())
            return not pending and not mismatched
    return False


def _action_fits(action, target, ref):
    step, op, attempt = ref.split("/")
    if action == "retry_step":
        return f"{step}/{attempt}" == target and op == "create"
    if action == "resume":
        return op == "create"
    if action == "cleanup_orphan":
        return f"{step}/{attempt}" == target and op == "delete"
    return op == "delete"  # escalate only compensates


def _check_approvals(f, d):
    """Walk each audit trail once; return what each approved action did, for the report."""
    windows = {}
    for env_id, env in d["envs"].items():
        h, window, agent_started = _History(), None, False
        for e in d["audit"].get(env_id, []):
            rid = env["request_id"]
            if e["kind"] == "policy_decision":
                agent_started = True
                decision = d["decisions"].get(e["ref"])
                if decision is None:
                    f.add("P3_decision_not_stored", f"{rid} {e['ref']}")
                    continue
                if decision["action"] not in ALLOWED_ACTIONS:
                    f.add("P1_action_not_allowlisted", f"{rid} {decision['action']}")
                if (e["details"].get("action"), e["details"].get("allowed")) != \
                        (decision["action"], decision["allowed"]):
                    f.add("P3_audit_disagrees_with_decision", f"{rid} {e['ref']}")
                if window is not None:
                    f.add("P2_overlapping_actions", f"{rid} {e['ref']} during {window['id']}")
                if decision["allowed"]:
                    if not _judge(h, decision["action"], decision["target"]):
                        f.add("P4_approval_not_justified",
                              f"{rid} {decision['action']} {decision['target']} in {h.state}")
                    window = {"id": e["ref"], "decision": decision, "attempts": [],
                              "changes": 0}
                continue
            if e["kind"] == "action_completed":
                if window is None or window["id"] != e["ref"]:
                    f.add("P3_completion_without_approval", f"{rid} {e['ref']}")
                else:
                    if window["changes"] == 0:
                        f.add("P3_approved_action_did_nothing", f"{rid} {e['ref']}")
                    windows[e["ref"]] = {"completed": e, "env_state": h.state,
                                         "attempts": window["attempts"],
                                         "owned": h.owned(env_id)}
                    if h.state in EMPTY_FINAL_STATES and h.owned(env_id):
                        f.add("P5_cleanup_claimed_with_resources", f"{rid} {e['ref']}")
                window = None
                continue
            if e["actor"] == "agent":
                if window is None:
                    f.add("P2_agent_change_without_approval", f"{rid} seq {e['seq']} {e['kind']}")
                else:
                    window["changes"] += 1
                    if e["kind"] in ("op_intent", "op_outcome") and not _action_fits(
                            window["decision"]["action"], window["decision"]["target"],
                            e["ref"]):
                        f.add("P2_change_outside_approved_scope",
                              f"{rid} {e['ref']} under {window['id']}")
                    if e["kind"] == "op_outcome":
                        window["attempts"].append({"ref": e["ref"],
                                                   "outcome": e["details"].get("outcome"),
                                                   "error_class": e["details"].get("error_class")})
            elif window is not None or (agent_started and e["kind"] != "duplicate_ignored"):
                f.add("P2_workflow_change_in_agent_scope", f"{rid} seq {e['seq']} {e['kind']}")
            h.apply(e)
        if window is not None:
            f.add("P3_approved_action_unfinished", f"{env['request_id']} {window['id']}")
    # One run's decisions must all come from one agent rules file.
    digests = {(x["inputs"] or {}).get("agent_rules_sha256") for x in d["decisions"].values()}
    if None in digests or len(digests) > 1:
        f.add("P6_agent_rules_inconsistent", f"{len(digests)} digests: {sorted(map(str, digests))}")
    audited = {e["ref"] for events in d["audit"].values() for e in events
               if e["kind"] == "policy_decision"}
    for decision_id in set(d["decisions"]) - audited:
        f.add("P3_decision_not_audited", decision_id)
    return windows


# The agent report must agree with the evidence

def _check_report(f, d, report_rows, windows):
    order = []
    for env in sorted(d["envs"].values(), key=lambda e: e["source_line"]):
        for e in d["audit"].get(env["env_id"], []):
            if e["kind"] == "policy_decision" and e["ref"] in d["decisions"]:
                order.append(e["ref"])
    reported = [r.get("decision_id") for r in report_rows]
    if reported != order:
        f.add("Q1_report_does_not_match_decisions",
              f"{len(reported)} reported, {len(order)} decided")
    for r in report_rows:
        decision = d["decisions"].get(r.get("decision_id"))
        if decision is None:
            f.add("Q1_reported_decision_unknown", str(r.get("decision_id")))
            continue
        env = d["envs"].get(decision["env_id"], {})
        if (r.get("env_id"), r.get("request_id"), r.get("action"), r.get("target"),
                r.get("allowed"), r.get("rule")) != (
                decision["env_id"], env.get("request_id"), decision["action"],
                decision["target"], decision["allowed"], decision["rule"]):
            f.add("Q2_report_row_disagrees", decision["decision_id"])
        if r.get("evidence") != decision["inputs"]:
            f.add("Q2_evidence_disagrees", decision["decision_id"])
        window = windows.get(decision["decision_id"])
        if r.get("executed") != (window is not None):
            f.add("Q3_executed_flag_wrong", decision["decision_id"])
        if window is None:
            if r.get("outcome") is not None:
                f.add("Q3_outcome_for_unexecuted_action", decision["decision_id"])
            continue
        outcome = r.get("outcome") or {}
        if outcome != window["completed"]["details"].get("outcome"):
            f.add("Q3_outcome_disagrees_with_audit", decision["decision_id"])
        derived = {"env_state": window["env_state"], "attempts": window["attempts"],
                   "owned_resources": window["owned"]}
        if outcome != derived:
            f.add("Q4_outcome_not_supported_by_evidence", decision["decision_id"])
        if outcome.get("env_state") in EMPTY_FINAL_STATES and outcome.get("owned_resources"):
            f.add("Q4_report_claims_cleanup_with_resources", decision["decision_id"])


# Optional: the injected faults really happened and had the required effect

def _check_faults(f, d, faults):
    env_of = {e["request_id"]: e["env_id"] for e in d["envs"].values()}
    attempts = {(a["env_id"], a["step"], a["op"], a["attempt"]): a for a in d["attempts"]}
    for fault in faults:
        env_id = env_of.get(fault.get("request_id"))
        a = attempts.get((env_id, fault.get("step"), fault.get("op"), fault.get("attempt")))
        expected = EXPECTED_FAULT_OUTCOME.get((fault.get("op"), fault.get("kind")))
        if a is None:
            f.add("K1_fault_not_exercised", json.dumps(fault, sort_keys=True))
        elif (a["outcome"], a["error_class"]) != expected:
            f.add("K1_fault_outcome_wrong", json.dumps(fault, sort_keys=True))
        if fault.get("kind") == "quota" and env_id in d["envs"] and \
                d["envs"][env_id]["state"] != "escalated":
            f.add("K2_quota_not_escalated", fault["request_id"])
    timeouts = sum(1 for c in d["cloud_log"] if c["response"] == "timeout")
    injected = sum(1 for x in faults if x.get("kind") == "timeout_after_success")
    if timeouts != injected:
        f.add("K3_timeout_after_success_count", f"log {timeouts}, injected {injected}")


# Command line

def read_jsonl(path):
    rows = []
    with open(path) as fh:
        for n, line in enumerate(fh, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path} line {n}: {e.msg}")
    return rows


def read_requests(path):
    with open(path) as fh:
        return [(n, json.loads(line)) for n, line in enumerate(fh, start=1) if line.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="checker.check")
    parser.add_argument("--dump", required=True)
    parser.add_argument("--report", required=True,
                        help="agent_report.jsonl from an agent run that exited 0")
    parser.add_argument("--requests")
    parser.add_argument("--faults")
    args = parser.parse_args(argv)
    try:
        dump_rows = read_jsonl(args.dump)
        report_rows = read_jsonl(args.report)
        requests = read_requests(args.requests) if args.requests else None
        faults = read_jsonl(args.faults) if args.faults else None
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    violations = check(dump_rows, report_rows, requests, faults)
    for v in violations:
        print(json.dumps(v, sort_keys=True))
    print(json.dumps({"violations": len(violations), "environments": sum(
        1 for r in dump_rows if r.get("table") == "environments"),
        "decisions": len(report_rows)}, sort_keys=True), file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
