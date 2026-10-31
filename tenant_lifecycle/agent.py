"""Deterministic ops agent. Not a model: it follows agent_rules.json.

The rules file lists, for each state, which actions to propose and in what
order. The code only works out each action's exact target from the registry.
Each proposal goes through the policy, which decides and stores the decision
first. Approved actions run through the `execute` callable supplied by the
caller, so this module never has access to the cloud.
"""

import hashlib
import json
import os

from tenant_lifecycle import models, policy

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_rules.json")
RULE_STATES = (models.PENDING, models.PROVISIONING, models.STEP_FAILED, models.CLEANUP_FAILED)


def load_rules(path=RULES_PATH):
    """Read and validate the rules file. Returns (rules, sha256 of its canonical JSON)."""
    with open(path) as f:
        try:
            rules = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON ({e.msg})")
    if not isinstance(rules, dict):
        raise ValueError(f"{path}: expected an object of state -> actions")
    for state, actions in rules.items():
        if state not in RULE_STATES:
            raise ValueError(f"{path}: unknown state {state!r}")
        if not isinstance(actions, list) or len(set(map(str, actions))) != len(actions):
            raise ValueError(f"{path}: {state} needs a list of distinct actions")
        unknown = [a for a in actions if not isinstance(a, str) or a not in _TARGETS]
        if unknown:
            raise ValueError(f"{path}: {state} has unknown actions {unknown}")
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return rules, hashlib.sha256(canonical.encode()).hexdigest()


def candidates(reg, env, rules):
    """Actions the rules propose for one environment, in the rules' order."""
    out = []
    for action in rules.get(env["state"], []):
        target = _TARGETS[action](reg, env["env_id"])
        if target is not None:
            out.append((action, target))
    return out


def run(reg, execute, rules, rules_digest):
    # Decisions record the rules that produced them; resuming with other rules
    # would propose different actions than the interrupted run.
    recorded = {json.loads(d["inputs"]).get("agent_rules_sha256") for d in reg.decisions()}
    if recorded - {rules_digest}:
        raise ValueError("agent_rules.json changed since this state's decisions were recorded")
    # Finish actions a crash interrupted before proposing anything new.
    for decision in reg.pending_actions():
        execute(decision["decision_id"])
    for env in reg.environments():
        _drive(reg, execute, env["env_id"], rules, rules_digest)


def report(reg):
    """One line per decision: what was proposed, the evidence, the rule, what happened."""
    rows = []
    for d in reg.decisions():
        completion = reg.completion(d["decision_id"])
        rows.append({"env_id": d["env_id"], "request_id": d["request_id"],
                     "decision_id": d["decision_id"], "action": d["action"],
                     "target": d["target"], "allowed": bool(d["allowed"]), "rule": d["rule"],
                     "evidence": json.loads(d["inputs"]),
                     "executed": completion is not None,
                     "outcome": completion["outcome"] if completion else None})
    return rows


def _drive(reg, execute, env_id, rules, rules_digest):
    while True:
        decision = None
        for action, target in candidates(reg, reg.get_environment(env_id), rules):
            decision = policy.decide(reg, env_id, action, target, rules_digest)
            if decision["allowed"]:
                break
        else:
            return
        # Every executed action changes the next proposal. Repeating one means
        # no progress, so stop loudly instead of looping.
        if reg.is_completed(decision["decision_id"]):
            raise RuntimeError(f"{decision['decision_id']} was proposed again after completing")
        execute(decision["decision_id"])
        if not reg.is_completed(decision["decision_id"]):
            raise RuntimeError(f"{decision['decision_id']} did not complete")


def _retry_target(reg, env_id):
    step, last = policy.failed_step(reg, env_id)
    return f"{step}/{last['attempt'] + 1}" if last is not None else None


def _cleanup_target(reg, env_id):
    step, last = policy.failed_deletion(reg, env_id)
    return f"{step}/{last['attempt'] + 1}" if step is not None else None


def _resume_target(reg, env_id):
    """Name the resume after the latest attempt, so each resume point has its own decision."""
    creates = [a for a in reg.attempts(env_id) if a["op"] == "create"]
    if not creates:
        return "start"
    latest = max(creates, key=lambda a: (models.STEPS.index(a["step"]), a["attempt"]))
    return f"{latest['step']}/{latest['attempt']}"


# How each action's exact target is worked out; the rules file only chooses and orders them.
_TARGETS = {"retry_step": _retry_target, "resume": _resume_target,
            "cleanup_orphan": _cleanup_target, "escalate": policy.escalate_target}
