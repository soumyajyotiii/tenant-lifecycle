"""Deterministic ops agent. A fixed decision rule, not a model.

The agent reads only the registry and proposes allowlisted actions. Each
proposal goes through the policy, which stores the decision first. Approved
actions run through the `execute` callable supplied by the caller, so this
module never has access to the cloud.
"""

from tenant_lifecycle import models, policy


def propose(reg, env):
    """Return (action, target) for one environment, or None."""
    env_id = env["env_id"]
    if env["state"] == models.STEP_FAILED:
        step, last = policy.failed_step(reg, env_id)
        if last is not None:
            return "retry_step", f"{step}/{last['attempt'] + 1}"
    # Only resume environments the agent already owns; provision resumes its own.
    if env["state"] == models.PROVISIONING and reg.has_decisions(env_id):
        return "resume", _resume_target(reg, env_id)
    return None


def run(reg, execute):
    # Finish actions a crash interrupted before proposing anything new.
    for decision in reg.pending_actions():
        execute(decision["decision_id"])
    for env in reg.environments():
        _drive(reg, execute, env["env_id"])


def report(reg):
    return [{"env_id": d["env_id"], "request_id": d["request_id"],
             "decision_id": d["decision_id"], "action": d["action"], "target": d["target"],
             "allowed": bool(d["allowed"]), "rule": d["rule"],
             "completed": bool(d["completed"])}
            for d in reg.decisions()]


def _drive(reg, execute, env_id):
    while True:
        proposal = propose(reg, reg.get_environment(env_id))
        if proposal is None:
            return
        decision = policy.decide(reg, env_id, *proposal)
        if not decision["allowed"]:
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
