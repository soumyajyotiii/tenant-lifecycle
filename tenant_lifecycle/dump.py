"""Deterministic JSONL export of the registry and inventory files.

Both files are opened read-only, so dumping can never change state. Every
line is one row tagged with its table; tables come in a fixed order and rows
by primary key. Stored JSON text is expanded so the dump is plain data.
"""

import json
import pathlib
import sqlite3

# (file, table, ORDER BY, columns holding JSON text)
TABLES = (
    ("registry", "environments", "env_id", ()),
    ("registry", "step_attempts", "env_id, step, op, attempt", ()),
    ("registry", "resources", "name", ()),
    ("registry", "policy_decisions", "decision_id", ("inputs",)),
    ("registry", "audit", "env_id, seq", ("details",)),
    ("inventory", "inventory", "name", ()),
    ("inventory", "cloud_log", "seq", ()),
)
BOOLEAN_COLUMNS = {("policy_decisions", "allowed")}


def dump_lines(registry_path, inventory_path):
    paths = {"registry": registry_path, "inventory": inventory_path}
    conns = {name: _open_read_only(path) for name, path in paths.items()}
    try:
        for db, table, order, json_columns in TABLES:
            conns[db].row_factory = sqlite3.Row
            for row in conns[db].execute(f"SELECT * FROM {table} ORDER BY {order}"):
                record = {"table": table}
                for key in row.keys():
                    value = row[key]
                    if key in json_columns:
                        value = json.loads(value)
                    elif (table, key) in BOOLEAN_COLUMNS:
                        value = bool(value)
                    record[key] = value
                yield json.dumps(record, sort_keys=True, separators=(",", ":"))
    finally:
        for conn in conns.values():
            conn.close()


def _open_read_only(path):
    try:
        uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        raise ValueError(f"cannot open {path} read-only; run provision first")
