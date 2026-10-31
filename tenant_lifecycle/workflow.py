"""Provisioning workflow: requests in file order, five steps per environment.

The workflow makes one attempt per step. It never retries a failed step and
never deletes. A step that does not end PRESENT stops the environment in
step_failed, for the agent to handle. Approved agent actions run through
execute(), so the agent itself never touches the cloud.
"""

import json

from tenant_lifecycle import models
from tenant_lifecycle.cloud import CloudError

ACTOR = "workflow"
AGENT = "agent"
REQUIRED_FIELDS = ("request_id", "tenant", "tier", "region")


class RequestFileError(ValueError):
    pass


def parse_requests(path):
    """Read and validate the whole file before anything is written."""
    requests = []
    with open(path) as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as e:
                raise RequestFileError(f"line {line_no}: invalid JSON ({e.msg})")
            if not isinstance(payload, dict):
                raise RequestFileError(f"line {line_no}: expected a JSON object")
            for key in REQUIRED_FIELDS:
                value = payload.get(key)
                if not isinstance(value, str) or not value:
                    raise RequestFileError(f"line {line_no}: {key} must be a non-empty string")
            if payload["tier"] not in models.TIERS:
                raise RequestFileError(f"line {line_no}: unknown tier {payload['tier']!r}")
            requests.append((line_no, payload))
    return requests


def provision(reg, cloud, requests):
    for line_no, payload in requests:
        env_id, status = reg.register_request(
            payload["request_id"], line_no, payload["tenant"], payload["tier"],
            payload["region"], payload, ACTOR)
        # Once the agent has acted on an environment, only the agent moves it on.
        if status != "duplicate" and not reg.has_decisions(env_id):
            advance(reg, cloud, env_id)


def advance(reg, cloud, env_id, actor=ACTOR):
    """Move one environment forward. Safe to call again after a crash."""
    env = reg.get_environment(env_id)
    if env["state"] == models.PENDING:
        reg.transition(env_id, models.PROVISIONING, actor)
    elif env["state"] != models.PROVISIONING:
        return

    for step in models.STEPS:
        last = _last_attempt(reg, env_id, step)
        if last is not None and last["outcome"] == models.PRESENT:
            continue
        if last is None or last["outcome"] is None:
            attempt = reg.next_attempt(env_id, step, "create") if last is None else last["attempt"]
            outcome, error = run_create(reg, cloud, env, step, attempt, actor)
        else:
            # The outcome was recorded but the crash came before step_failed.
            outcome, error = last["outcome"], last["error_class"]
        if outcome != models.PRESENT:
            reg.transition(env_id, models.STEP_FAILED, actor,
                           reason=_failure_reason(step, outcome, error))
            return
    reg.transition(env_id, models.ACTIVE, actor)


def execute(reg, cloud, decision_id):
    """Run one approved agent action. Safe to call again after a crash."""
    decision = reg.get_decision(decision_id)
    if decision is None or not decision["allowed"]:
        raise PermissionError(f"{decision_id} is not approved")
    if decision["action"] == "retry_step":
        _retry_step(reg, cloud, decision)
    elif decision["action"] == "resume":
        advance(reg, cloud, decision["env_id"], AGENT)
    else:
        raise PermissionError(f"{decision['action']} has no executor")
    reg.complete_action(decision_id)


def _retry_step(reg, cloud, decision):
    env_id = decision["env_id"]
    step, _, attempt = decision["target"].partition("/")
    attempt = int(attempt)
    row = next((a for a in reg.attempts(env_id)
                if (a["step"], a["op"], a["attempt"]) == (step, "create", attempt)), None)
    if row is None or row["outcome"] is None:
        if reg.get_environment(env_id)["state"] == models.STEP_FAILED:
            reg.transition(env_id, models.PROVISIONING, AGENT,
                           reason=f"retry_step {step}/{attempt}")
        outcome, error = run_create(reg, cloud, reg.get_environment(env_id), step, attempt, AGENT)
    else:
        outcome, error = row["outcome"], row["error_class"]
    if outcome != models.PRESENT and reg.get_environment(env_id)["state"] == models.PROVISIONING:
        reg.transition(env_id, models.STEP_FAILED, AGENT,
                       reason=_failure_reason(step, outcome, error))


def run_create(reg, cloud, env, step, attempt, actor=ACTOR):
    """Record intent, call the cloud only if nothing is there, record what the inventory shows."""
    env_id = env["env_id"]
    name = models.resource_name(env_id, step)
    expected = (name, env_id, step, models.resource_kind(step, env["tier"]),
                models.resource_parent(step, env["tier"], env["region"]))

    reg.record_intent(env_id, step, "create", attempt, actor)
    error = None
    # After a crash the earlier call may already have succeeded, so look first.
    if cloud.observe(*expected) == models.ABSENT:
        try:
            _call_create(cloud, env, step, name, attempt)
        except CloudError as e:
            error = e.error_class
    outcome = cloud.observe(*expected)
    if outcome != models.ABSENT:
        error = None
    reg.record_outcome(env_id, step, "create", attempt, outcome, error, actor)
    return outcome, error


def _call_create(cloud, env, step, name, attempt):
    if step == "database":
        cloud.create_database(name, env["env_id"], env["tier"], env["region"], attempt)
    else:
        getattr(cloud, f"create_{step}")(name, env["env_id"], attempt)


def _last_attempt(reg, env_id, step):
    rows = [a for a in reg.attempts(env_id) if a["step"] == step and a["op"] == "create"]
    return rows[-1] if rows else None


def _failure_reason(step, outcome, error):
    return f"{step}: {outcome}" + (f" ({error})" if error else "")
