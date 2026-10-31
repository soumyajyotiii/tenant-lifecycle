"""Policy for agent actions. Every decision is stored before anything executes.

The policy reads only the registry. It never trusts the agent's view: it
recomputes the facts from durable state and records them with the decision.
"""

from tenant_lifecycle import models

ALLOWED_ACTIONS = ("retry_step", "resume", "cleanup_orphan", "escalate")
MAX_RETRIES = 2
MAX_ATTEMPTS = 1 + MAX_RETRIES
MAX_DELETE_ATTEMPTS = 3
RETRYABLE_ERRORS = frozenset({"timeout", "already_exists"})


def decision_id(env_id, action, target):
    return f"{env_id}:{action}:{target}"


def decide(reg, env_id, action, target, rules_digest):
    """Evaluate and durably record a decision. Returns the stored decision row.

    `rules_digest` identifies the agent rules that proposed the action. It is
    kept as evidence only; it never changes the verdict.
    """
    did = decision_id(env_id, action, target)
    existing = reg.get_decision(did)
    if existing is not None:
        return existing
    allowed, rule, inputs = evaluate(reg, env_id, action, target)
    inputs["agent_rules_sha256"] = rules_digest
    return reg.record_decision(did, env_id, action, target, allowed, rule, inputs)


def evaluate(reg, env_id, action, target):
    """Return (allowed, rule, inputs). Pure: reads the registry, writes nothing."""
    env = reg.get_environment(env_id)
    inputs = {"state": env["state"] if env else None}
    if action not in ALLOWED_ACTIONS:
        return False, "action_not_allowed", inputs
    if env is None:
        return False, "unknown_environment", inputs
    if action == "retry_step":
        return _evaluate_retry(reg, env, target, inputs)
    if action == "resume":
        return _evaluate_resume(reg, env, inputs)
    if action == "cleanup_orphan":
        return _evaluate_cleanup(reg, env, target, inputs)
    return _evaluate_escalate(reg, env, inputs)


def failed_step(reg, env_id):
    """The first step whose latest create attempt is not PRESENT, with that attempt."""
    for step in models.STEPS:
        last = _last(reg, env_id, step, "create")
        if last is None or last["outcome"] != models.PRESENT:
            return step, last
    return None, None


def failed_deletion(reg, env_id):
    """The first resource, in reverse step order, still present after a failed delete."""
    for step in reversed(models.STEPS):
        res = _resource(reg, env_id, step)
        last = _last(reg, env_id, step, "delete")
        if (res is not None and res["status"] == models.PRESENT and last is not None
                and last["outcome"] == models.PRESENT):
            return step, last
    return None, None


def escalate_target(reg, env_id):
    """Name each escalation after the latest attempt, so every round has its own decision."""
    return reg.latest_intent(env_id) or "start"


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
    # Open attempts are allowed: resume reconciles each against the inventory
    # before any cloud call, exactly as a provision rerun would.
    unresolved = reg.open_attempts(env["env_id"])
    inputs["open_attempts"] = len(unresolved)
    if env["state"] not in (models.PENDING, models.PROVISIONING):
        return False, "env_not_provisioning", inputs
    if any(a["op"] != "create" for a in unresolved):
        return False, "unresolved_delete", inputs
    return True, "resume_allowed", inputs


def _evaluate_cleanup(reg, env, target, inputs):
    step, _, attempt = target.partition("/")
    res = _resource(reg, env["env_id"], step) if step in models.STEPS else None
    last = _last(reg, env["env_id"], step, "delete") if res is not None else None
    inputs.update({"resource": res["name"] if res else None,
                   "resource_status": res["status"] if res else None,
                   "last_delete_attempt": last["attempt"] if last else None,
                   "last_delete_outcome": last["outcome"] if last else None,
                   "error_class": last["error_class"] if last else None,
                   "max_delete_attempts": MAX_DELETE_ATTEMPTS})
    if env["state"] != models.CLEANUP_FAILED:
        return False, "env_not_cleanup_failed", inputs
    if res is None:
        return False, "resource_not_accounted", inputs
    if res["status"] != models.PRESENT:
        # Foreign and mismatched resources are never ours to delete.
        return False, "resource_not_deletable", inputs
    if last is None or last["outcome"] != models.PRESENT:
        return False, "no_failed_deletion", inputs
    if attempt != str(last["attempt"] + 1):
        return False, "attempt_out_of_order", inputs
    if last["attempt"] + 1 > MAX_DELETE_ATTEMPTS:
        return False, "cleanup_budget_exhausted", inputs
    return True, "cleanup_allowed", inputs


def _evaluate_escalate(reg, env, inputs):
    env_id = env["env_id"]
    resources = reg.resources(env_id)
    inputs.update({
        "present": [r["name"] for r in resources if r["status"] == models.PRESENT],
        "mismatched": [r["name"] for r in resources if r["status"] == models.MISMATCH],
        "open_attempts": len(reg.open_attempts(env_id)),
    })
    if env["state"] == models.STEP_FAILED:
        step, last = failed_step(reg, env_id)
        retry_target = f"{step}/{last['attempt'] + 1}" if last else f"{step}/1"
        retry_allowed, retry_rule, _ = _evaluate_retry(reg, env, retry_target, {})
        inputs["retry_rule"] = retry_rule
        if retry_allowed:
            return False, "retry_still_possible", inputs
        return True, "escalate_allowed", inputs
    if env["state"] == models.CLEANUP_FAILED:
        step, _ = failed_deletion(reg, env_id)
        inputs["failed_deletion"] = step
        if step is not None:
            return False, "failed_deletion_pending", inputs
        if inputs["mismatched"]:
            return False, "unexpected_resource_needs_human", inputs
        return True, "escalate_allowed", inputs
    return False, "env_not_escalatable", inputs


def _last(reg, env_id, step, op):
    rows = [a for a in reg.attempts(env_id) if a["step"] == step and a["op"] == op]
    return rows[-1] if rows else None


def _resource(reg, env_id, step):
    name = models.resource_name(env_id, step)
    return next((r for r in reg.resources(env_id) if r["name"] == name), None)
