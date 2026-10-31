"""Domain constants, lifecycle states, legal transitions and deterministic identity."""

import hashlib
import json

STEPS = ("namespace", "database", "secret", "dns", "app")
TIERS = ("basic", "enterprise")
ACTORS = ("workflow", "agent", "human")
OPS = ("create", "delete")

# Basic tenants get a logical database on their region's shared cluster. The
# cluster is only a name, not an inventory resource, so nothing can ever
# compensate it away.
SHARED_CLUSTER_PREFIX = "shared-cluster"

PENDING = "pending"
PROVISIONING = "provisioning"
STEP_FAILED = "step_failed"
ACTIVE = "active"
COMPENSATING = "compensating"
CLEANUP_FAILED = "cleanup_failed"
ESCALATED = "escalated"
CLEANED_UP = "cleaned_up"

TRANSITIONS = {
    PENDING: {PROVISIONING},
    PROVISIONING: {ACTIVE, STEP_FAILED},
    STEP_FAILED: {PROVISIONING, COMPENSATING},
    COMPENSATING: {ESCALATED, CLEANED_UP, CLEANUP_FAILED},
    CLEANUP_FAILED: {COMPENSATING},
    ACTIVE: set(),
    ESCALATED: set(),
    CLEANED_UP: set(),
}
STATES = tuple(TRANSITIONS)
TERMINAL_STATES = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Creates only move an environment forward; deletes only happen while cleaning up.
OP_ALLOWED_STATES = {
    "create": frozenset({PROVISIONING}),
    "delete": frozenset({COMPENSATING, CLEANUP_FAILED}),
}

# Outcomes record what the inventory showed after a call, never the cloud's reply.
PRESENT = "present"
ABSENT = "absent"
FOREIGN_OWNER = "foreign_owner"
OUTCOMES = (PRESENT, ABSENT, FOREIGN_OWNER)

# The outcome that means "the call changed nothing". Only then is the cloud's
# error class safe to store: no cloud write happened, so no crash could have
# hidden the reply.
NO_CHANGE_OUTCOME = {"create": ABSENT, "delete": PRESENT}


def is_legal_transition(from_state, to_state):
    return to_state in TRANSITIONS.get(from_state, ())


def env_id_for(request_id):
    return "env-" + hashlib.sha256(request_id.encode()).hexdigest()[:12]


def resource_name(env_id, step):
    return f"{env_id}-{step}"


def resource_kind(step, tier):
    if step == "database":
        return "logical_db" if tier == "basic" else "dedicated_db"
    return step


def shared_cluster(region):
    return f"{SHARED_CLUSTER_PREFIX}-{region}"


def resource_parent(step, tier, region):
    return shared_cluster(region) if step == "database" and tier == "basic" else None


def payload_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


RESOURCE_KINDS = frozenset(resource_kind(step, tier) for step in STEPS for tier in TIERS)
