"""Policy for agent actions. Every decision is stored before anything executes.

The policy reads only the registry. It never trusts the agent's view: it
recomputes the facts from durable state and records them with the decision.
"""

from tenant_lifecycle import models

ALLOWED_ACTIONS = ("retry_step", "resume", "cleanup_orphan", "escalate")
ENABLED_ACTIONS = frozenset({"retry_step", "resume"})
MAX_RETRIES = 2
MAX_ATTEMPTS = 1 + MAX_RETRIES
RETRYABLE_ERRORS = frozenset({"timeout", "already_exists"})


def decision_id(env_id, action, target):
    return f"{env_id}:{action}:{target}"


def decide(reg, env_id, action, target):
    """Evaluate and durably record a decision. Returns the stored decision row."""
    did = decision_id(env_id, action, target)
    existing = reg.get_decision(did)
    if existing is not None:
        return existing
    allowed, rule, inputs = evaluate(reg, env_id, action, target)
    return reg.record_decision(did, env_id, action, target, allowed, rule, inputs)


def evaluate(reg, env_id, action, target):
    """Return (allowed, rule, inputs). Pure: reads the registry, writes nothing."""
    env = reg.get_environment(env_id)
    inputs = {"state": env["state"] if env else None}
    if action not in ALLOWED_ACTIONS:
        return False, "action_not_allowed", inputs
    if action not in ENABLED_ACTIONS:
        return False, "action_not_implemented", inputs
    if env is None:
        return False, "unknown_environment", inputs
    if action == "retry_step":
        return _evaluate_retry(reg, env, target, inputs)
    return _evaluate_resume(reg, env, inputs)


def failed_step(reg, env_id):
    """The first step whose latest create attempt is not PRESENT, with that attempt."""
    for step in models.STEPS:
        rows = [a for a in reg.attempts(env_id) if a["step"] == step and a["op"] == "create"]
        last = rows[-1] if rows else None
        if last is None or last["outcome"] != models.PRESENT:
            return step, last
    return None, None


def _evaluate_retry(reg, env, target, inputs):
    step, _, attempt = target.partition("/")
    expected_step, last = failed_step(reg, env["env_id"])
    inputs.update({"failed_step": expected_step, "max_attempts": MAX_ATTEMPTS,
                   "last_attempt": last["attempt"] if last else None,
                   "outcome": last["outcome"] if last else None,
                   "error_class": last["error_class"] if last else None})
    if env["state"] != models.STEP_FAILED:
        return False, "env_not_step_failed", inputs
    if last is None or step != expected_step:
        return False, "not_the_failed_step", inputs
    if last["outcome"] != models.ABSENT:
        return False, "outcome_not_retryable", inputs
    if last["error_class"] == "quota":
        return False, "quota_not_retryable", inputs
    if last["error_class"] not in RETRYABLE_ERRORS:
        return False, "error_not_retryable", inputs
    if attempt != str(last["attempt"] + 1):
        return False, "attempt_out_of_order", inputs
    if last["attempt"] + 1 > MAX_ATTEMPTS:
        return False, "retry_budget_exhausted", inputs
    return True, "retry_allowed", inputs


def _evaluate_resume(reg, env, inputs):
    unresolved = reg.open_attempts(env["env_id"])
    inputs["open_attempts"] = len(unresolved)
    if env["state"] != models.PROVISIONING:
        return False, "env_not_provisioning", inputs
    if unresolved:
        return False, "unresolved_attempt", inputs
    return True, "resume_allowed", inputs
