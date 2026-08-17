**Title:** `fix: IndexLock must not wedge on a filesystem that cannot flock`

**Branch:** `iret77:fix/harden-index-lock` (projected on demand from our fork's
equivalent fix) · **Depends on:** nothing (⊘) · **Against:** your 0.4.3 `IndexLock`

---

This is the **one** overlap with your `0.4.3` #106 work that we think is still
worth a PR — not our lock, just a robustness gap in yours we already hit and
fixed on the fork.

### The bug

`IndexLock.try_acquire()` collapses every failure into "held by someone else":

```python
fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)   # not guarded
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:                                               # any errno
    os.close(fd)
    return False
```

Two ways this bites, both silent:

1. **A filesystem that can't lock.** Some network / FUSE home directories answer
   `ENOLCK` or `ENOTSUP` from `flock`. That is not contention — but it returns
   `False`, so an index-passive instance retries **forever** and a CLI
   `index`/`rebuild` waits out its timeout and exits. The index becomes
   **permanently unwritable**, with "another instance is syncing" as the only
   explanation.
2. **A read-only home.** `os.open(..., O_CREAT)` raises, and nothing catches it —
   `try_acquire()` propagates `OSError` to every caller of `serve` / `index`.

### The fix

Distinguish genuine contention from an unlockable filesystem, and degrade to
single-process (with a WARNING) instead of wedging:

```python
import errno   # add to imports

def try_acquire(self) -> bool:
    if self._fd is not None:
        return True
    try:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # Read-only home, etc. Refusing here would wedge every write
        # forever, so degrade to single-process — but say so, rather
        # than returning the False a real contention returns.
        self._degraded = True
        logger.warning(
            "Cannot create index lock file %s (%s); a second process's "
            "writes are NOT prevented.", self.lock_path, exc,
        )
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            os.close(fd)          # genuinely held — the case this lock is for
            return False
        # flock unsupported here (ENOLCK / ENOTSUP on some network homes).
        # Treating that as "held" made the index permanently unwritable.
        self._degraded = True
        logger.warning(
            "Filesystem holding %s does not support locking (%s); a "
            "second process's writes are NOT prevented.", self.lock_path, exc,
        )
        self._fd = fd
        return True
    self._fd = fd
    return True
```

(`self._degraded = False` in `__init__`; optionally expose it so callers that
promise cross-process exclusivity can see the degradation.)

### Worth pushing back on

Degrading to single-process is a deliberate choice: on a filesystem where
`flock` does not work, the alternatives are to wedge (current behaviour) or to
raise on every start. Single-process + a loud WARNING keeps a single-instance
user working — which is the common case — and only loses the guarantee for the
rarer two-instance case, where the log says exactly what was lost.

### Changelog

```markdown
### Fixed

- **`IndexLock` no longer wedges on a filesystem that cannot `flock`.** A network
  or FUSE home answering `ENOLCK`/`ENOTSUP`, or a read-only home, was treated as
  lock contention, so the index became permanently unwritable ("another instance
  is syncing" forever). Genuine contention (`EWOULDBLOCK`/`EAGAIN`/`EACCES`) is
  now distinguished from an unlockable filesystem, which degrades to
  single-process with a warning.
```

### Verification

The lock path is pure `fcntl`/`sqlite` and runs off-macOS:

```
uv run pytest tests/test_lock.py -q     # incl. a simulated ENOLCK case
uv run ruff check src/                  # All checks passed!
```
