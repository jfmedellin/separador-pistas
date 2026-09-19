"""Cross-process single-instance guard backed by an OS-managed exclusive lock.

ARC-01: today every launch runs `recover_unfinished()` unconditionally, so a
second instance can corrupt a first instance's live `preparing`/`processing`
rows. `acquire_single_instance()` gives `main()` a way to detect a second
launch and exit before touching `HistoryStore` at all.

The lock is held by keeping the file handle open for this process's entire
lifetime (D9): it is never explicitly closed. The OS releases the lock
automatically when this process ends, by any means, including a crash or a
forced kill -- unlike a presence-only sentinel file, which would leak after
a hard kill and permanently block every later launch.
"""

from __future__ import annotations

import sys
from pathlib import Path

from SeparationWorker.paths import local_data_root

_LOCK_FILE_NAME = "instance.lock"

# Kept alive for the lifetime of this process so the OS-held lock is never
# released early. Intentionally never closed by this module.
_held_lock_handle = None


def acquire_single_instance(root: Path | None = None) -> bool:
    """Attempt to become the sole owner of the instance lock under `root`.

    Returns True when this process now exclusively holds the lock, False
    when another process already holds it. On success, the open file
    handle is kept for the rest of this process's lifetime; the OS
    releases the lock automatically on process termination.
    """
    global _held_lock_handle

    base = root if root is not None else local_data_root()
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / _LOCK_FILE_NAME

    handle = open(lock_path, "a+b")

    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

    _held_lock_handle = handle
    return True
