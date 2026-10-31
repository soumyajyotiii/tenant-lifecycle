"""Simulated cloud. The only writer of the inventory SQLite file.

Faults are a pure function of (env_id, step, op, attempt). The cloud keeps no
record of which faults fired, so resending the same attempt after a crash hits
the same fault again. The plan is stored once in the inventory file, so the
agent sees the same faults as provision and a different plan is rejected.
"""

import json
import sqlite3

from tenant_lifecycle import models, writes

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    name         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    owner_env_id TEXT NOT NULL,
    owner_step   TEXT NOT NULL,
    parent       TEXT
);

CREATE TABLE IF NOT EXISTS cloud_log (
    seq          INTEGER PRIMARY KEY,
    op           TEXT NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    owner_env_id TEXT NOT NULL,
    response     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fault_plan (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    plan TEXT NOT NULL
);
"""

TIMEOUT = "timeout"
TIMEOUT_AFTER_SUCCESS = "timeout_after_success"
QUOTA = "quota"
DUPLICATE = "duplicate"
FAULT_KINDS = (TIMEOUT, TIMEOUT_AFTER_SUCCESS, QUOTA, DUPLICATE)
CREATE_ONLY_FAULTS = frozenset({QUOTA})


class CloudError(Exception):
    error_class = "cloud_error"


class CloudTimeout(CloudError):
    error_class = "timeout"


class QuotaExceeded(CloudError):
    error_class = "quota"


class AlreadyExists(CloudError):
    error_class = "already_exists"


class NotFound(CloudError):
    error_class = "not_found"


class OwnershipMismatch(CloudError):
    error_class = "ownership_mismatch"


class FaultPlanMismatch(ValueError):
    pass


def parse_faults(records):
    """Turn faults.jsonl records into {(env_id, step, op, attempt): kind}."""
    plan = {}
    for i, rec in enumerate(records, start=1):
        try:
            request_id, step, op = rec["request_id"], rec["step"], rec["op"]
            attempt, kind = rec["attempt"], rec["kind"]
        except (KeyError, TypeError):
            raise ValueError(f"fault {i}: needs request_id, step, op, attempt and kind")
        if step not in models.STEPS or op not in models.OPS:
            raise ValueError(f"fault {i}: unknown step or op")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError(f"fault {i}: attempt must be a positive integer")
        if kind not in FAULT_KINDS:
            raise ValueError(f"fault {i}: unknown kind {kind!r}")
        if kind in CREATE_ONLY_FAULTS and op != "create":
            raise ValueError(f"fault {i}: {kind} only applies to create")
        key = (models.env_id_for(request_id), step, op, attempt)
        if key in plan:
            raise ValueError(f"fault {i}: second fault for the same attempt")
        plan[key] = kind
    return plan


def load_faults(path):
    with open(path) as f:
        return parse_faults(json.loads(line) for line in f if line.strip())


def _encode_plan(plan):
    return json.dumps(sorted([*key, kind] for key, kind in plan.items()))


def _decode_plan(text):
    return {(env_id, step, op, attempt): kind
            for env_id, step, op, attempt, kind in json.loads(text)}


class Cloud:
    def __init__(self, path, faults=None):
        """Use `faults` if given; otherwise the plan stored in the file, if any."""
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if faults is None:
            stored = self._stored_plan()
            faults = _decode_plan(stored) if stored is not None else {}
        self.faults = dict(faults)

    def close(self):
        self.conn.close()

    def install_faults(self, plan):
        """Store the plan on first use. Later runs must pass the same plan."""
        encoded, stored = _encode_plan(plan), self._stored_plan()
        if stored is None:
            with writes.transaction(self.conn):
                self.conn.execute("INSERT INTO fault_plan (id, plan) VALUES (1, ?)", (encoded,))
        elif stored != encoded:
            raise FaultPlanMismatch("faults differ from the plan stored in the inventory")
        self.faults = dict(plan)

    def _stored_plan(self):
        row = self.conn.execute("SELECT plan FROM fault_plan WHERE id = 1").fetchone()
        return row["plan"] if row else None

    def create(self, name, kind, owner_env_id, owner_step, parent, attempt):
        if kind not in models.RESOURCE_KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if (kind == "logical_db") != (parent is not None):
            raise ValueError("only a logical_db has a parent cluster")
        fault = self.faults.get((owner_env_id, owner_step, "create", attempt))
        if fault == QUOTA:
            raise QuotaExceeded(name)
        if fault == TIMEOUT:
            raise CloudTimeout(name)
        if fault == DUPLICATE or self.get(name) is not None:
            raise AlreadyExists(name)

        response = TIMEOUT if fault == TIMEOUT_AFTER_SUCCESS else "ok"
        with writes.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO inventory VALUES (?, ?, ?, ?, ?)",
                (name, kind, owner_env_id, owner_step, parent),
            )
            self._log("create", name, kind, owner_env_id, response)
        if fault == TIMEOUT_AFTER_SUCCESS:
            raise CloudTimeout(name)

    def delete(self, name, owner_env_id, owner_step, attempt):
        fault = self.faults.get((owner_env_id, owner_step, "delete", attempt))
        if fault == TIMEOUT:
            raise CloudTimeout(name)
        row = self.get(name)
        if fault == DUPLICATE or row is None:
            raise NotFound(name)

        response = TIMEOUT if fault == TIMEOUT_AFTER_SUCCESS else "ok"
        with writes.transaction(self.conn):
            # The owner condition is part of the delete itself, so a foreign
            # resource can never be removed.
            cur = self.conn.execute(
                "DELETE FROM inventory WHERE name = ? AND owner_env_id = ? AND owner_step = ?",
                (name, owner_env_id, owner_step),
            )
            if cur.rowcount != 1:
                raise OwnershipMismatch(name)
            self._log("delete", name, row["kind"], owner_env_id, response)
        if fault == TIMEOUT_AFTER_SUCCESS:
            raise CloudTimeout(name)

    # The five provisioning operations and their deletes.

    def create_namespace(self, name, owner_env_id, attempt):
        self.create(name, "namespace", owner_env_id, "namespace", None, attempt)

    def create_database(self, name, owner_env_id, tier, region, attempt):
        self.create(name, models.resource_kind("database", tier), owner_env_id, "database",
                    models.resource_parent("database", tier, region), attempt)

    def create_secret(self, name, owner_env_id, attempt):
        self.create(name, "secret", owner_env_id, "secret", None, attempt)

    def create_dns(self, name, owner_env_id, attempt):
        self.create(name, "dns", owner_env_id, "dns", None, attempt)

    def create_app(self, name, owner_env_id, attempt):
        self.create(name, "app", owner_env_id, "app", None, attempt)

    def delete_namespace(self, name, owner_env_id, attempt):
        self.delete(name, owner_env_id, "namespace", attempt)

    def delete_database(self, name, owner_env_id, attempt):
        self.delete(name, owner_env_id, "database", attempt)

    def delete_secret(self, name, owner_env_id, attempt):
        self.delete(name, owner_env_id, "secret", attempt)

    def delete_dns(self, name, owner_env_id, attempt):
        self.delete(name, owner_env_id, "dns", attempt)

    def delete_app(self, name, owner_env_id, attempt):
        self.delete(name, owner_env_id, "app", attempt)

    def get(self, name):
        return self.conn.execute("SELECT * FROM inventory WHERE name = ?", (name,)).fetchone()

    def observe(self, name, owner_env_id, owner_step, kind, parent):
        """Compare the inventory row for this name with the resource we expect.

        Recovery accepts a resource only when it is PRESENT, meaning owner,
        step, kind and parent all match.
        """
        row = self.get(name)
        if row is None:
            return models.ABSENT
        if row["owner_env_id"] != owner_env_id:
            return models.FOREIGN_OWNER
        if (row["owner_step"], row["kind"], row["parent"]) != (owner_step, kind, parent):
            return models.MISMATCH
        return models.PRESENT

    def inventory(self):
        return self.conn.execute("SELECT * FROM inventory ORDER BY name").fetchall()

    def owned_by(self, owner_env_id):
        """Names of every resource this owner still has, whatever its kind or step."""
        return [r["name"] for r in self.conn.execute(
            "SELECT name FROM inventory WHERE owner_env_id = ? ORDER BY name", (owner_env_id,))]

    def log(self):
        return self.conn.execute("SELECT * FROM cloud_log ORDER BY seq").fetchall()

    def _log(self, op, name, kind, owner_env_id, response):
        self.conn.execute(
            "INSERT INTO cloud_log (op, name, kind, owner_env_id, response) VALUES (?, ?, ?, ?, ?)",
            (op, name, kind, owner_env_id, response),
        )
