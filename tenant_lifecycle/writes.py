"""The one transaction helper for both SQLite files, and the CRASH_AFTER write counter.

A write is one committed transaction on either file. With CRASH_AFTER=n the
process exits immediately after the n-th commit, without running any cleanup.
"""

import os
from contextlib import contextmanager

CRASH_EXIT_CODE = 3

_committed = 0


def committed_count():
    return _committed


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    _count_write()


def _count_write():
    global _committed
    _committed += 1
    limit = _crash_limit()
    if limit is not None and _committed >= limit:
        os._exit(CRASH_EXIT_CODE)


def _crash_limit():
    raw = os.environ.get("CRASH_AFTER")
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"CRASH_AFTER must be a positive integer, got {raw!r}")
    return int(raw)
