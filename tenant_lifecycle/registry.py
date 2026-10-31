"""Durable registry and audit trail. The only writer of the registry SQLite file."""

import json
import sqlite3

from tenant_lifecycle import models, writes

SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
    env_id       TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL UNIQUE,
    source_line  INTEGER NOT NULL,
    tenant       TEXT NOT NULL,
    tier         TEXT NOT NULL,
    region       TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS step_attempts (
    env_id      TEXT NOT NULL REFERENCES environments(env_id),
    step        TEXT NOT NULL,
    op          TEXT NOT NULL,
    attempt     INTEGER NOT NULL CHECK (attempt >= 1),
    actor       TEXT NOT NULL,
    outcome     TEXT,
    error_class TEXT,
    PRIMARY KEY (env_id, step, op, attempt)
);

CREATE TABLE IF NOT EXISTS resources (
    name   TEXT PRIMARY KEY,
    env_id TEXT NOT NULL REFERENCES environments(env_id),
    step   TEXT NOT NULL,
    kind   TEXT NOT NULL,
    parent TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    env_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ref     TEXT NOT NULL,
    details TEXT NOT NULL,
    PRIMARY KEY (env_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS audit_once ON audit (env_id, kind, ref)
    WHERE kind IN ('request_registered', 'duplicate_ignored', 'op_intent', 'op_outcome');

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""


class IllegalTransition(Exception):
    pass


class RegistryConflict(Exception):
    pass


class Registry:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def _tx(self):
        return writes.transaction(self.conn)

    def _audit(self, env_id, actor, kind, ref, details):
        (seq,) = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit WHERE env_id = ?", (env_id,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO audit (env_id, seq, actor, kind, ref, details) VALUES (?, ?, ?, ?, ?, ?)",
            (env_id, seq, actor, kind, ref, json.dumps(details, sort_keys=True)),
        )

    # Requests

    def register_request(self, request_id, line_no, tenant, tier, region, payload,
                         actor="workflow"):
        """Return (env_id, status) where status is 'created', 'existing' or 'duplicate'.

        The first line carrying a request_id owns the environment. Seeing that
        same line again is a replay and writes nothing. Any other line with the
        same request_id is audited once as a duplicate and never applied.
        """
        _check_actor(actor)
        if tier not in models.TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        if not isinstance(region, str) or not region:
            raise ValueError(f"region must be a non-empty string, got {region!r}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        fields = {"request_id": request_id, "tenant": tenant, "tier": tier, "region": region}
        for key, value in fields.items():
            if payload.get(key) != value:
                raise ValueError(f"{key} {value!r} does not match payload {payload.get(key)!r}")
        env_id = models.env_id_for(request_id)
        digest = models.payload_hash(payload)
        row = self.get_environment(env_id)

        if row is None:
            with self._tx():
                self.conn.execute(
                    "INSERT INTO environments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (env_id, request_id, line_no, tenant, tier, region, digest, models.PENDING),
                )
                self._audit(env_id, actor, "request_registered", f"line:{line_no}",
                            {"request_id": request_id, "tenant": tenant, "tier": tier,
                             "region": region, "payload_hash": digest})
            return env_id, "created"

        if row["source_line"] == line_no:
            if row["payload_hash"] != digest:
                raise RegistryConflict(f"line {line_no} changed payload for {request_id}")
            return env_id, "existing"

        if self._has_audit(env_id, "duplicate_ignored", f"line:{line_no}"):
            return env_id, "duplicate"
        with self._tx():
            self._audit(env_id, actor, "duplicate_ignored", f"line:{line_no}",
                        {"request_id": request_id, "original_line": row["source_line"],
                         "same_payload": digest == row["payload_hash"]})
        return env_id, "duplicate"

    # Lifecycle

    def transition(self, env_id, to_state, actor, reason=None):
        _check_actor(actor)
        row = self._require_environment(env_id)
        from_state = row["state"]
        if not models.is_legal_transition(from_state, to_state):
            raise IllegalTransition(f"{env_id}: {from_state} -> {to_state}")
        # An open attempt may have changed the cloud. Its outcome must be
        # reconciled and recorded before the lifecycle moves on.
        unresolved = self.open_attempts(env_id)
        if unresolved:
            a = unresolved[0]
            raise RegistryConflict(
                f"{env_id}: {_ref(a['step'], a['op'], a['attempt'])} is unresolved")
        with self._tx():
            cur = self.conn.execute(
                "UPDATE environments SET state = ? WHERE env_id = ? AND state = ?",
                (to_state, env_id, from_state),
            )
            if cur.rowcount != 1:
                raise IllegalTransition(f"{env_id}: state changed during transition")
            self._audit(env_id, actor, "state_changed", f"{from_state}->{to_state}",
                        {"from": from_state, "to": to_state, "reason": reason})

    # Operation tracking

    def next_attempt(self, env_id, step, op):
        (n,) = self.conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 FROM step_attempts"
            " WHERE env_id = ? AND step = ? AND op = ?",
            (env_id, step, op),
        ).fetchone()
        return n

    def record_intent(self, env_id, step, op, attempt, actor):
        """Durably record a cloud call before it is made. Returns False if already recorded."""
        _check_actor(actor)
        _check_step_op(step, op)
        env = self._require_environment(env_id)
        existing = self._get_attempt(env_id, step, op, attempt)
        if existing is not None:
            if existing["actor"] != actor:
                raise RegistryConflict(f"{_ref(step, op, attempt)} belongs to {existing['actor']}")
            return False

        if env["state"] not in models.OP_ALLOWED_STATES[op]:
            raise RegistryConflict(f"{op} not allowed in state {env['state']}")
        open_row = self.conn.execute(
            "SELECT attempt FROM step_attempts"
            " WHERE env_id = ? AND step = ? AND op = ? AND outcome IS NULL",
            (env_id, step, op),
        ).fetchone()
        if open_row is not None:
            raise RegistryConflict(f"attempt {open_row['attempt']} of {step}/{op} is still open")
        if attempt != self.next_attempt(env_id, step, op):
            raise RegistryConflict(f"attempt {attempt} is out of order for {step}/{op}")

        name = models.resource_name(env_id, step)
        resource = self._get_resource(name)
        if op == "delete" and resource is None:
            raise RegistryConflict(f"refusing to delete unaccounted resource {name}")
        if op == "create" and resource is not None and resource["status"] == models.PRESENT:
            raise RegistryConflict(f"{name} is already present")

        with self._tx():
            self.conn.execute(
                "INSERT INTO step_attempts (env_id, step, op, attempt, actor) VALUES (?, ?, ?, ?, ?)",
                (env_id, step, op, attempt, actor),
            )
            if op == "create":
                self.conn.execute(
                    "INSERT INTO resources VALUES (?, ?, ?, ?, ?, 'intended')"
                    " ON CONFLICT (name) DO UPDATE SET status = 'intended'",
                    (name, env_id, step, models.resource_kind(step, env["tier"]),
                     models.resource_parent(step, env["tier"], env["region"])),
                )
            self._audit(env_id, actor, "op_intent", _ref(step, op, attempt),
                        {"step": step, "op": op, "attempt": attempt, "resource": name})
        return True

    def record_outcome(self, env_id, step, op, attempt, outcome, error_class, actor):
        """Record what the inventory showed after the call. Returns False if already recorded."""
        _check_actor(actor)
        _check_step_op(step, op)
        if outcome not in models.OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        if (outcome == models.NO_CHANGE_OUTCOME[op]) != (error_class is not None):
            raise ValueError("error_class is required exactly when the call changed nothing")

        row = self._get_attempt(env_id, step, op, attempt)
        if row is None:
            raise RegistryConflict(f"no intent for {_ref(step, op, attempt)}")
        if row["actor"] != actor:
            raise RegistryConflict(f"{_ref(step, op, attempt)} belongs to {row['actor']}")
        if row["outcome"] is not None:
            if (row["outcome"], row["error_class"]) == (outcome, error_class):
                return False
            raise RegistryConflict(f"{_ref(step, op, attempt)} already has outcome {row['outcome']}")

        with self._tx():
            self.conn.execute(
                "UPDATE step_attempts SET outcome = ?, error_class = ?"
                " WHERE env_id = ? AND step = ? AND op = ? AND attempt = ?",
                (outcome, error_class, env_id, step, op, attempt),
            )
            self.conn.execute(
                "UPDATE resources SET status = ? WHERE name = ?",
                (outcome, models.resource_name(env_id, step)),
            )
            self._audit(env_id, actor, "op_outcome", _ref(step, op, attempt),
                        {"step": step, "op": op, "attempt": attempt,
                         "outcome": outcome, "error_class": error_class})
        return True

    # Reads

    def get_environment(self, env_id):
        return self.conn.execute(
            "SELECT * FROM environments WHERE env_id = ?", (env_id,)
        ).fetchone()

    def state_counts(self):
        return dict(self.conn.execute(
            "SELECT state, COUNT(*) FROM environments GROUP BY state ORDER BY state"
        ).fetchall())

    def open_attempts(self, env_id):
        return self.conn.execute(
            "SELECT * FROM step_attempts WHERE env_id = ? AND outcome IS NULL"
            " ORDER BY step, op, attempt",
            (env_id,),
        ).fetchall()

    def attempts(self, env_id):
        return self.conn.execute(
            "SELECT * FROM step_attempts WHERE env_id = ? ORDER BY step, op, attempt",
            (env_id,),
        ).fetchall()

    def resources(self, env_id):
        return self.conn.execute(
            "SELECT * FROM resources WHERE env_id = ? ORDER BY name", (env_id,)
        ).fetchall()

    def audit_events(self, env_id):
        return self.conn.execute(
            "SELECT * FROM audit WHERE env_id = ? ORDER BY seq", (env_id,)
        ).fetchall()

    def _require_environment(self, env_id):
        row = self.get_environment(env_id)
        if row is None:
            raise KeyError(env_id)
        return row

    def _get_attempt(self, env_id, step, op, attempt):
        return self.conn.execute(
            "SELECT * FROM step_attempts WHERE env_id = ? AND step = ? AND op = ? AND attempt = ?",
            (env_id, step, op, attempt),
        ).fetchone()

    def _get_resource(self, name):
        return self.conn.execute("SELECT * FROM resources WHERE name = ?", (name,)).fetchone()

    def _has_audit(self, env_id, kind, ref):
        return self.conn.execute(
            "SELECT 1 FROM audit WHERE env_id = ? AND kind = ? AND ref = ?", (env_id, kind, ref)
        ).fetchone() is not None


def _check_actor(actor):
    if actor not in models.ACTORS:
        raise ValueError(f"unknown actor {actor!r}")


def _check_step_op(step, op):
    if step not in models.STEPS:
        raise ValueError(f"unknown step {step!r}")
    if op not in models.OPS:
        raise ValueError(f"unknown op {op!r}")


def _ref(step, op, attempt):
    return f"{step}/{op}/{attempt}"
