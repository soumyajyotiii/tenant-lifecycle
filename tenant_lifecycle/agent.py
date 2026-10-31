"""Deterministic ops agent. A fixed decision rule, not a model.

The agent reads only the registry and proposes allowlisted actions. Each
proposal goes through the policy, which stores the decision first. Approved
actions run through the `execute` callable supplied by the caller, so this
module never has access to the cloud.
"""

import json

from tenant_lifecycle import models, policy


def candidates(reg, env):
    """Actions worth proposing for one environment, most preferred first."""
    env_id, state = env["env_id"], env["state"]
    if state == models.STEP_FAILED:
        step, last = policy.failed_step(reg, env_id)
        out = [("retry_step", f"{step}/{last['attempt'] + 1}")] if last is not None else []
        return out + [("escalate", policy.escalate_target(reg, env_id))]
    if state == models.CLEANUP_FAILED:
        step, last = policy.failed_deletion(reg, env_id)
        out = [("cleanup_orphan", f"{step}/{last['attempt'] + 1}")] if step is not None else []
        return out + [("escalate", policy.escalate_target(reg, env_id))]
    # Includes environments a crashed provision left behind, open attempts and all.
    if state in (models.PENDING, models.PROVISIONING):
        return [("resume", _resume_target(reg, env_id))]
    return []


def run(reg, execute):
    # Finish actions a crash interrupted before proposing anything new.
    for decision in reg.pending_actions():
        execute(decision["decision_id"])
    for env in reg.environments():
        _drive(reg, execute, env["env_id"])


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


def _drive(reg, execute, env_id):
    while True:
        decision = None
        for action, target in candidates(reg, reg.get_environment(env_id)):
            decision = policy.decide(reg, env_id, action, target)
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


def _resume_target(reg, env_id):
    """Name the resume after the latest attempt, so each resume point has its own decision."""
    creates = [a for a in reg.attempts(env_id) if a["op"] == "create"]
    if not creates:
        return "start"
    latest = max(creates, key=lambda a: (models.STEPS.index(a["step"]), a["attempt"]))
    return f"{latest['step']}/{latest['attempt']}"
