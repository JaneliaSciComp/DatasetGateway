"""Serialize bounded token issuance on SQLite and retry lock contention."""

from contextlib import nullcontext
from threading import Lock
from time import sleep

from django.db import OperationalError, connection

_SQLITE_WRITE_LOCK = Lock()


def run_serialized_write(fn, *args, **kwargs):
    """Run a transactional callable, retrying the entire rolled-back write.

    SQLite has no per-user row locks. Serialize writes within this process;
    the retry handles a writer in another process. Other databases rely on
    the callable's transaction and row locks.
    """
    lock = _SQLITE_WRITE_LOCK if connection.vendor == "sqlite" else nullcontext()
    with lock:
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except OperationalError as error:
                sqlite_lock_error = (
                    connection.vendor == "sqlite"
                    and "locked" in str(error).lower()
                )
                if not sqlite_lock_error or attempt == 4:
                    raise
                sleep(0.01 * (attempt + 1))
