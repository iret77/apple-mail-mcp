"""IndexManager - Central interface for the FTS5 search index.

Provides:
- build_from_disk(): Pre-index emails by reading .emlx files directly
- sync_updates(): Incremental sync via JXA for new emails
- search(): Fast FTS5 search with BM25 ranking
- get_stats(): Index statistics for status reporting

Thread Safety:
- Uses threading.Lock for connection management
- Database connections use check_same_thread=False
- File watcher runs in separate thread with its own connection
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..config import (
    get_index_max_emails,
    get_index_path,
    get_index_staleness_hours,
)
from .schema import (
    INSERT_EMAIL_SQL,
    init_database,
    insert_attachments,
    optimize_fts_index,
    rebuild_fts_index,
)
from .search import SearchResult  # Re-use, don't duplicate

if TYPE_CHECKING:
    from collections.abc import Callable

    from .watcher import IndexWatcher

logger = logging.getLogger(__name__)

# How many lifecycle events to keep in memory for the status tool.
MAX_EVENTS = 50


class IndexBusyError(RuntimeError):
    """A build or sync is already running.

    Distinct from a failure: nothing is wrong, the caller simply must
    not present the skipped work as completed.
    """


@dataclass
class IndexStats:
    """Statistics about the search index."""

    email_count: int
    mailbox_count: int
    last_sync: datetime | None
    db_size_mb: float
    staleness_hours: float | None
    capped_mailboxes: int = 0
    attachment_count: int = 0
    disk_email_count: int | None = None
    failed_jobs_count: int = 0
    excluded_accounts: list[str] = field(default_factory=list)


# SearchResult is imported from .search to avoid duplication


class IndexManager:
    """
    Manages the FTS5 search index for email body search.

    The index is stored at ~/.apple-mail-mcp/index.db by default.
    Use environment variables to customize:
    - APPLE_MAIL_INDEX_PATH: Database location
    - APPLE_MAIL_INDEX_MAX_EMAILS: Optional per-mailbox cap (default: uncapped)
    - APPLE_MAIL_INDEX_STALENESS_HOURS: Hours before stale (24)

    Thread Safety:
    - get_instance() uses class-level lock
    - _get_conn() uses instance-level lock
    - Watcher runs in separate thread with its own connection
    """

    _instance: IndexManager | None = None
    _instance_lock = threading.Lock()

    # Cache TTL for the disk inventory walk in get_stats(). The walk
    # is O(N files) and can dominate response latency on >100k
    # mailboxes (#78). 60s is generous — disk_email_count is a
    # coverage gauge, not a security-critical value.
    _DISK_COUNT_TTL_SEC: float = 60.0

    def __init__(self, db_path: Path | None = None):
        """
        Initialize the IndexManager.

        Args:
            db_path: Custom database path (uses config default if None)
        """
        self._db_path = db_path or get_index_path()
        # One connection per thread (see _get_conn): a shared connection
        # would make every query wait behind a running build or sync.
        self._local = threading.local()
        self._open_conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        # SQLite allows a single writer. A build and a sync running at
        # once fight over it: one wipes rows the other just wrote, and
        # the loser dies on "database is locked". Both take this lock.
        self._write_lock = threading.Lock()
        self._watcher: IndexWatcher | None = None
        self._watcher_callback: Callable[[int, int], None] | None = None
        # (count, expiry_monotonic) — None until first successful read.
        self._disk_count_cache: tuple[int, float] | None = None
        # Excluded-account UUIDs from the most recent resolution
        # (build/sync/watcher). Lets JXA-free paths like the disk
        # email count honor exclusions without their own JXA call.
        self._exclude_account_uuids: set[str] = set()
        # Observability for the status tool: a build runs in a daemon
        # thread, so "is it working or wedged?" is otherwise invisible
        # to the caller. Both are process-local by design — they
        # describe *this* server's activity.
        self._building = False
        self._last_error: str | None = None
        # Build heartbeat. Records the phase, how much has been read
        # and written, and when that last changed. The phase matters:
        # a build spends its first minutes reading Apple's metadata
        # before a single row is written, and without naming that
        # phase, "warming up" and "wedged" look identical — both show
        # zero indexed messages.
        self._build_progress: dict | None = None
        # Ring of recent lifecycle events. The server's stderr is not
        # visible to a user running it as a desktop extension, so this
        # is the only channel through which "what just happened?" can
        # be answered — the status tool reads it.
        self._events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._events_lock = threading.Lock()

    def write_lock_held(self) -> bool:
        """True while a build or sync holds the single-writer lock."""
        return self._write_lock.locked()

    def record_event(self, level: str, message: str, **fields) -> None:
        """Record a lifecycle event and mirror it to the logger.

        Two audiences: the log file for a post-mortem after a restart,
        and this ring for "what is happening right now", which the
        status tool surfaces. Never raises — diagnostics must not be
        able to break the operation they describe.
        """
        try:
            event = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "level": level,
                "message": message,
                # Coerce: these go straight into an MCP response, and a
                # non-serializable value would break the whole status
                # reply rather than this one event.
                **{k: str(v) for k, v in fields.items()},
            }
            with self._events_lock:
                self._events.append(event)
            logger.log(
                logging.ERROR if level == "error" else logging.INFO,
                "%s%s",
                message,
                f" {fields}" if fields else "",
            )
        except Exception:  # pragma: no cover - diagnostics only
            pass

    def recent_events(self, limit: int = 20) -> list[dict]:
        """Most recent lifecycle events, newest first."""
        with self._events_lock:
            events = list(self._events)
        return list(reversed(events[-limit:]))

    def is_building(self) -> bool:
        """True while a full index build is running in this process."""
        return self._building

    # Reading Apple's metadata for a large mailbox legitimately takes
    # minutes with nothing to show, so it gets a longer grace period
    # than the indexing loop, which should tick every few seconds.
    _STALL_SECONDS: ClassVar[dict[str, float]] = {
        "clearing": 120.0,
        "reading_metadata": 600.0,
        "indexing": 120.0,
    }

    def build_progress(self) -> dict | None:
        """Heartbeat of the running build, or None if none is running.

        Returns ``phase`` ("reading_metadata" or "indexing"),
        ``emails_done``, ``files_seen``, ``seconds_since_progress`` and
        ``appears_stalled``. The phase is what makes zero progress
        interpretable: during metadata reading, zero is expected.
        """
        progress = self._build_progress
        if progress is None:
            return None
        phase = progress["phase"]
        idle = max(0.0, time.monotonic() - progress["ts"])
        return {
            "phase": phase,
            "emails_done": progress["done"],
            "files_seen": progress["seen"],
            "seconds_since_progress": round(idle, 1),
            "appears_stalled": idle > self._STALL_SECONDS.get(phase, 120.0),
        }

    def _mark_progress(
        self, phase: str, *, done: int = 0, seen: int = 0
    ) -> None:
        """Stamp the build heartbeat."""
        self._build_progress = {
            "phase": phase,
            "done": done,
            "seen": seen,
            "ts": time.monotonic(),
        }

    @property
    def last_error(self) -> str | None:
        """Most recent build/sync failure message, or None.

        Cleared on the next successful build/sync so a transient
        failure (e.g. Full Disk Access granted mid-session) doesn't
        stick around misreporting a healthy index.
        """
        return self._last_error

    def indexed_email_count(self) -> int:
        """Cheap COUNT(*) over the emails table (no disk walk).

        Distinguishes "DB file exists" from "DB has content" — an
        interrupted or permission-denied first build leaves an empty
        database behind, which must not be mistaken for a usable index.
        """
        try:
            row = (
                self._get_conn()
                .execute("SELECT COUNT(*) AS n FROM emails")
                .fetchone()
            )
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0

    def has_usable_index(self) -> bool:
        """True when an index exists *and* holds at least one email."""
        return self.has_index() and self.indexed_email_count() > 0

    def _resolve_exclusions(self) -> set[str]:
        """Resolve configured account exclusions to UUIDs and remember
        them for JXA-free consumers (see ``_exclude_account_uuids``).
        """
        from .accounts import resolve_excluded_account_uuids

        self._exclude_account_uuids = resolve_excluded_account_uuids()
        return self._exclude_account_uuids

    @classmethod
    def get_instance(cls) -> IndexManager:
        """Get the singleton IndexManager instance (thread-safe)."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = IndexManager()
            return cls._instance

    @property
    def db_path(self) -> Path:
        """Get the database file path."""
        return self._db_path

    def _get_conn(self) -> sqlite3.Connection:
        """Get this thread's database connection, opening it if needed.

        Connections are **per thread**, not shared. A single shared
        connection serializes every statement behind whatever long
        operation currently holds it: a full rebuild or a disk sync runs
        for minutes, and any status or search query issued meanwhile
        would block for that entire time — the server looks hung. SQLite
        in WAL mode is built for the opposite: one writer plus any
        number of concurrent readers, each on its own connection, with
        readers served from a consistent snapshot instead of waiting.

        ``init_database`` is idempotent — after the first call it only
        reads the schema version — so per-thread setup stays cheap.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Serialize first-time setup: concurrent threads must not
            # race on creating the schema or running a migration.
            with self._conn_lock:
                conn = init_database(self._db_path)
            self._local.conn = conn
            with self._conn_lock:
                self._open_conns.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this manager has opened.

        Called by tests and on explicit teardown. Threads that later ask
        for a connection get a fresh one.
        """
        with self._conn_lock:
            for conn in self._open_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._open_conns.clear()
        self._local = threading.local()

    def has_index(self) -> bool:
        """Check if an index database exists."""
        return self._db_path.exists()

    def get_stats(self) -> IndexStats:
        """
        Get index statistics.

        Returns:
            IndexStats with counts, size, and staleness info
        """
        conn = self._get_conn()

        # Email count
        cursor = conn.execute("SELECT COUNT(*) FROM emails")
        email_count = cursor.fetchone()[0]

        # Mailbox count
        cursor = conn.execute(
            "SELECT COUNT(DISTINCT account || '/' || mailbox) FROM emails"
        )
        mailbox_count = cursor.fetchone()[0]

        # Last sync time
        cursor = conn.execute("SELECT MAX(last_sync) FROM sync_state")
        row = cursor.fetchone()
        last_sync = None
        staleness_hours = None
        if row and row[0]:
            last_sync = datetime.fromisoformat(row[0])
            delta = (datetime.now() - last_sync).total_seconds()
            staleness_hours = delta / 3600

        # Database file size
        db_size_mb = 0.0
        if self._db_path.exists():
            db_size_mb = self._db_path.stat().st_size / (1024 * 1024)

        # Count mailboxes at or above the per-mailbox cap.
        # Default is uncapped (None) — only query when a cap is set.
        max_per_mailbox = get_index_max_emails()
        capped_mailboxes = 0
        if max_per_mailbox is not None:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT account, mailbox FROM emails"
                "  GROUP BY account, mailbox"
                "  HAVING COUNT(*) >= ?"
                ")",
                (max_per_mailbox,),
            )
            capped_mailboxes = cursor.fetchone()[0]

        # Attachment count
        cursor = conn.execute("SELECT COUNT(*) FROM attachments")
        attachment_count = cursor.fetchone()[0]

        # Failed parse jobs count (DLQ)
        # The table may not exist on a stale connection still on schema v4
        # — guard with try/except rather than coupling to schema version.
        failed_jobs_count = 0
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM failed_index_jobs")
            failed_jobs_count = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # Disk email count (best-effort, skip if no FDA). Cached
        # with a 60s TTL — the underlying disk walk is O(N files)
        # and would dominate latency for clients polling
        # `index://status` on a tight loop. (#78)
        disk_email_count = self._get_disk_email_count_cached()

        # Configured account exclusions (display names — no JXA needed
        # to report them; resolution to UUIDs happens at index time).
        from ..config import get_index_exclude_accounts

        excluded_accounts = sorted(get_index_exclude_accounts())

        return IndexStats(
            email_count=email_count,
            mailbox_count=mailbox_count,
            last_sync=last_sync,
            db_size_mb=db_size_mb,
            staleness_hours=staleness_hours,
            capped_mailboxes=capped_mailboxes,
            attachment_count=attachment_count,
            disk_email_count=disk_email_count,
            failed_jobs_count=failed_jobs_count,
            excluded_accounts=excluded_accounts,
        )

    def _get_disk_email_count_cached(self) -> int | None:
        """Return disk email count, walking the filesystem at most
        once per `_DISK_COUNT_TTL_SEC`. Returns None if Full Disk
        Access is not granted or the Mail directory is missing.
        """
        now = time.monotonic()
        cache = self._disk_count_cache
        if cache is not None and cache[1] > now:
            return cache[0]
        try:
            from .disk import find_mail_directory, get_disk_inventory

            mail_dir = find_mail_directory()
            # Honor exclusions (as last resolved — this path is
            # JXA-free by design) so the count matches what the
            # index is allowed to contain (#90).
            count = len(
                get_disk_inventory(
                    mail_dir,
                    exclude_account_uuids=self._exclude_account_uuids,
                )
            )
        except (FileNotFoundError, PermissionError):
            # Don't cache failures — the next call should retry in
            # case Full Disk Access has since been granted.
            return None
        self._disk_count_cache = (count, now + self._DISK_COUNT_TTL_SEC)
        return count

    def cached_disk_count(self) -> int | None:
        """Last known on-disk email count, without walking the disk.

        Lets the status tool show build progress while a build is
        running: the fresh walk would compete with the build for I/O,
        but a value from before it started is a perfectly good
        denominator.
        """
        cache = self._disk_count_cache
        return cache[0] if cache is not None else None

    def invalidate_disk_count_cache(self) -> None:
        """Drop the cached disk email count. Call after a sync or
        rebuild that materially changes on-disk state.
        """
        self._disk_count_cache = None

    def is_stale(self) -> bool:
        """Check if the index needs a sync."""
        stats = self.get_stats()
        if stats.staleness_hours is None:
            return True
        return stats.staleness_hours > get_index_staleness_hours()

    def build_from_disk(
        self,
        progress_callback: Callable[[int, int | None, str], None] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> int:
        """
        Build the index by reading .emlx files directly from disk.

        This requires Full Disk Access permission for the terminal.
        Much faster than fetching via JXA (~30x faster).

        Args:
            progress_callback: Optional callback(current, total, message)

        Returns:
            Number of emails indexed

        Raises:
            PermissionError: If Full Disk Access is not granted
            FileNotFoundError: If Mail directory not found
        """
        from .disk import (
            _infer_account_mailbox,
            find_mail_directory,
            scan_all_emails,
        )

        # Mark the build as running before the first thing that can
        # fail, so the status tool can distinguish "never started",
        # "running" and "failed with this error".
        # Only one build or sync may touch the database at a time.
        # SQLite gives us a single writer; two concurrent rebuilds would
        # each DELETE the other's rows. Acquired non-blocking so a
        # second caller is told "already running" instead of queueing.
        if not self._write_lock.acquire(blocking=False):
            raise IndexBusyError(
                "An index build or sync is already running; "
                "wait for it to finish."
            )

        # Everything below runs inside try/finally: the flag, the
        # dropped triggers and the emptied tables must be restored on
        # EVERY exit path. A failure between here and the bulk loop used
        # to leave `_building` stuck True (the server then reported
        # "building" forever) and the FTS triggers permanently dropped
        # (new mail silently stopped entering the search index).
        self._building = True
        # Phases are reported so a slow preparation step is never
        # mistaken for a hang.
        self._mark_progress("clearing")
        # Only now has the build truly begun: the lock is held and the
        # state is visible. Callers use this to distinguish a started
        # build from one refused on the first line.
        self.record_event("info", "Index build started")
        if on_started is not None:
            on_started()

        conn = None
        # Set when a swallowed error in the cleanup path means the
        # index is NOT actually complete — e.g. the FTS rebuild failed,
        # leaving rows in `emails` that body search can never find.
        finalize_error: str | None = None
        batch: list[tuple] = []
        # Deferred attachment rows: (email_tuple_index, attachments)
        batch_attachments: list[tuple[int, list]] = []
        batch_size = 500
        mailbox_counts: dict[tuple[str, str], int] = {}
        capped_mailboxes: set[tuple[str, str]] = set()
        total_indexed = 0

        try:
            # Verify we can access the mail directory
            try:
                mail_dir = find_mail_directory()
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                raise

            # Resolve excluded account names -> UUIDs (one JXA call,
            # only when exclusions are configured) so the JXA-free disk
            # walk can skip whole accounts. Excluded accounts never
            # enter the index.
            exclude_account_uuids = self._resolve_exclusions()

            conn = self._get_conn()
            max_per_mailbox = get_index_max_emails()

            # Drop the FTS triggers BEFORE clearing, not after. With
            # `emails_ad` still attached, `DELETE FROM emails` fires one
            # FTS5 delete per row — on a 60k index that is minutes of
            # work during which nothing is written, and it is entirely
            # wasted: the FTS content is rebuilt from scratch below.
            conn.execute("DROP TRIGGER IF EXISTS emails_ai")
            conn.execute("DROP TRIGGER IF EXISTS emails_ad")
            conn.execute("DROP TRIGGER IF EXISTS emails_au")

            # Clear existing data for rebuild
            conn.execute("DELETE FROM attachments")
            conn.execute("DELETE FROM emails")
            conn.execute("DELETE FROM sync_state")
            # Empty the FTS index in a single operation instead of the
            # per-row deletes the trigger would have done.
            conn.execute(
                "INSERT INTO emails_fts(emails_fts) VALUES('delete-all')"
            )

            def _record_skip(path: Path, reason: str) -> None:
                """Never drop a message without a trace: a skipped file
                goes into the DLQ so the status tool can explain the
                gap between disk and index counts."""
                try:
                    acct, mbox = _infer_account_mailbox(path, mail_dir)
                    from .schema import RECORD_PARSE_FAILURE_SQL, skip_row

                    conn.execute(
                        RECORD_PARSE_FAILURE_SQL,
                        skip_row(str(path), acct, mbox, reason),
                    )
                except Exception:
                    logger.debug("Could not record skip for %s", path)

            files_seen = 0
            self._mark_progress("reading_metadata")
            for email_data in scan_all_emails(
                mail_dir,
                exclude_account_uuids=exclude_account_uuids,
                on_skip=_record_skip,
            ):
                files_seen += 1
                # Tick well before the first batch commits, so a build
                # that is parsing steadily never looks frozen.
                if files_seen % 100 == 0:
                    self._mark_progress(
                        "indexing", done=total_indexed, seen=files_seen
                    )
                key = (email_data["account"], email_data["mailbox"])
                count = mailbox_counts.get(key, 0)

                if max_per_mailbox is not None and count >= max_per_mailbox:
                    capped_mailboxes.add(key)
                    continue

                mailbox_counts[key] = count + 1

                attachments = email_data.get("attachments", [])
                batch.append(
                    (
                        email_data["id"],
                        email_data["account"],
                        email_data["mailbox"],
                        email_data.get("subject", ""),
                        email_data.get("sender", ""),
                        email_data.get("content", ""),
                        email_data.get("date_received", ""),
                        email_data.get("emlx_path", ""),
                        email_data.get("message_id_header") or None,
                        len(attachments),
                    )
                )
                if attachments:
                    batch_attachments.append((len(batch) - 1, attachments))

                if len(batch) >= batch_size:
                    self._flush_batch(conn, batch, batch_attachments)
                    self._mark_progress(
                        "indexing",
                        done=total_indexed + len(batch),
                        seen=files_seen,
                    )
                    total_indexed += len(batch)

                    if progress_callback:
                        msg = f"Indexed {total_indexed} emails..."
                        progress_callback(total_indexed, None, msg)

                    batch = []
                    batch_attachments = []

        except BaseException as exc:
            # Record EVERY failure. Only the unreadable-mail-directory
            # case used to set _last_error, so any other exception left
            # the status cheerfully reporting "no errors" while the
            # build was dead.
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.record_event(
                "error", "Index build failed", error=self._last_error
            )
            # Abandon whatever the failed run had open, so the write
            # lock is not held for the rest of the process lifetime.
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    logger.debug("rollback after failed build failed")
            raise
        finally:
            # The build is no longer running, however it ended. Set
            # before the flush so a failure in cleanup can't leave the
            # status tool reporting a phantom in-progress build.
            self._building = False
            self._build_progress = None

            # NOTE: the write lock is released at the very END of this
            # block. Everything below still writes — the final flush and
            # the FTS rebuild are the heaviest writes in the program —
            # and releasing early let a waiting sync grab the lock, whose
            # open transaction then made these writes fail with
            # "database is locked" from inside this `finally`.

            # Nothing was opened (e.g. Mail unreadable): no schema
            # state to restore. Guarded with a conditional rather than
            # an early return — returning from `finally` would swallow
            # the exception that brought us here.
            if conn is not None:
                # Restore the triggers FIRST. DROP TRIGGER is DDL and
                # autocommits, so a rollback does not bring them back:
                # if anything below throws before this runs, the
                # triggers are gone from the database file permanently
                # and new mail silently stops entering the search
                # index. Nothing may precede this.
                self._recreate_fts_triggers(conn)

                # Best-effort from here: losing the tail of a build is
                # recoverable, breaking the schema is not.
                try:
                    # Flush any remaining partial batch (crash-safe)
                    if batch:
                        self._flush_batch(conn, batch, batch_attachments)
                        total_indexed += len(batch)

                    # Update sync state for whatever we managed to index
                    if mailbox_counts:
                        now = datetime.now().isoformat()
                        for (acct_, mbox_), count in mailbox_counts.items():
                            conn.execute(
                                """INSERT OR REPLACE INTO sync_state
                                 (account, mailbox, last_sync, message_count)
                                 VALUES (?, ?, ?, ?)""",
                                (acct_, mbox_, now, count),
                            )
                        conn.commit()
                except sqlite3.Error as exc:
                    finalize_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("Could not finalize index build")

                # Rebuild FTS index (must run even if scan crashed
                # mid-iteration, otherwise emails table has rows
                # but FTS5 is empty)
                if total_indexed > 0:
                    if progress_callback:
                        msg = "Building search index..."
                        progress_callback(total_indexed, total_indexed, msg)

                    try:
                        rebuild_fts_index(conn)
                        optimize_fts_index(conn)
                    except sqlite3.Error as exc:
                        finalize_error = f"{type(exc).__name__}: {exc}"
                        logger.exception("FTS rebuild failed")

                # Log cap warnings (aggregate summary)
                if capped_mailboxes:
                    logger.warning(
                        "%d mailbox(es) hit the per-mailbox cap (%d). "
                        "Increase APPLE_MAIL_INDEX_MAX_EMAILS to index more.",
                        len(capped_mailboxes),
                        max_per_mailbox,
                    )
                    if progress_callback:
                        msg = (
                            f"Warning: {len(capped_mailboxes)} mailbox(es) "
                            f"hit cap ({max_per_mailbox})"
                        )
                        progress_callback(total_indexed, total_indexed, msg)

            # Released last: every write above must be protected.
            self._write_lock.release()

        # Disk inventory just changed — drop the cache so the next
        # status call reflects truth.
        self.invalidate_disk_count_cache()
        if finalize_error:
            # Rows exist but the index is not usable as promised — a
            # failed FTS rebuild leaves body search permanently empty.
            # Reporting success here is how that stays invisible.
            self._last_error = finalize_error
            self.record_event(
                "error",
                "Index build finished with errors; search may be incomplete",
                error=finalize_error,
                emails=total_indexed,
            )
        else:
            self._last_error = None  # a clean build clears prior failures
            self.record_event(
                "info", "Index build finished", emails=total_indexed
            )
        return total_indexed

    @staticmethod
    def _recreate_fts_triggers(conn: sqlite3.Connection) -> None:
        """Restore the FTS sync triggers.

        `DROP TRIGGER` is DDL and autocommits, so a rollback never
        brings them back. If a build ends without running this, the
        triggers are gone from the database FILE — surviving restarts —
        and every later insert lands in `emails` but never in
        `emails_fts`: body search silently stops seeing new mail.
        """
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS emails_ai
            AFTER INSERT ON emails BEGIN
                INSERT INTO emails_fts(rowid, subject, sender, content)
                VALUES (new.rowid, new.subject, new.sender, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS emails_ad
            AFTER DELETE ON emails BEGIN
                INSERT INTO emails_fts(
                    emails_fts, rowid, subject, sender, content
                ) VALUES(
                    'delete', old.rowid, old.subject,
                    old.sender, old.content
                );
            END;

            CREATE TRIGGER IF NOT EXISTS emails_au
            AFTER UPDATE ON emails BEGIN
                INSERT INTO emails_fts(
                    emails_fts, rowid, subject, sender, content
                ) VALUES(
                    'delete', old.rowid, old.subject,
                    old.sender, old.content
                );
                INSERT INTO emails_fts(rowid, subject, sender, content)
                VALUES (new.rowid, new.subject, new.sender, new.content);
            END;
        """)

    @staticmethod
    def _flush_batch(
        conn: sqlite3.Connection,
        batch: list[tuple],
        batch_attachments: list[tuple[int, list]],
    ) -> None:
        """Insert a batch of emails and their attachment metadata.

        Attachment-bearing rows insert individually so
        cursor.lastrowid links the attachment rows without a
        post-hoc SELECT per email (INSERT OR REPLACE always yields
        a fresh rowid); the attachment-free majority keeps the
        executemany fast path.
        """
        with_attachments = {idx for idx, _ in batch_attachments}
        plain = [
            row for i, row in enumerate(batch) if i not in with_attachments
        ]
        if plain:
            conn.executemany(INSERT_EMAIL_SQL, plain)

        for idx, attachments in batch_attachments:
            cursor = conn.execute(INSERT_EMAIL_SQL, batch[idx])
            if cursor.lastrowid is not None:
                insert_attachments(conn, cursor.lastrowid, attachments)

        conn.commit()

    def sync_updates(
        self,
        progress_callback: Callable[[int, int | None, str], None] | None = None,
    ) -> int:
        """
        Sync index with disk using state reconciliation.

        Compares the filesystem with the database to detect:
        - New emails (on disk, not in DB)
        - Deleted emails (in DB, not on disk)
        - Moved emails (same ID, different path)

        This is much faster than the old JXA-based sync (~30x faster)
        and handles deletions correctly.

        Args:
            progress_callback: Optional callback(current, total, message)

        Returns:
            Number of changes (added + deleted + moved)
        """
        if not self._write_lock.acquire(blocking=False):
            # Distinguishable from "0 changes": the caller must not
            # report a sync that never ran as a successful no-op.
            self.record_event("info", "Sync skipped: index busy")
            raise IndexBusyError("An index build or sync is already running.")

        self.record_event("info", "Sync started")
        self._last_error = None  # this run's verdict, not a previous one
        try:
            changes = self._sync_updates_locked(progress_callback)
        except BaseException as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.record_event("error", "Sync failed", error=self._last_error)
            raise
        finally:
            self._write_lock.release()
        if self._last_error:
            # The Full-Disk-Access path reports a soft failure by
            # returning 0 rather than raising. Announcing "finished"
            # then puts a success at the top of the ring the model is
            # told to quote from — the exact lie this ring exists to
            # prevent.
            self.record_event(
                "error", "Sync did not run", error=self._last_error
            )
        else:
            self.record_event("info", "Sync finished", changes=changes)
        return changes

    def _sync_updates_locked(self, progress_callback=None) -> int:
        """Body of :meth:`sync_updates`, holding the write lock."""
        from .disk import find_mail_directory
        from .sync import sync_from_disk

        try:
            mail_dir = find_mail_directory()
        except (FileNotFoundError, PermissionError) as e:
            logger.warning("Cannot access mail directory for sync: %s", e)
            # Record it, but keep returning 0 (callers rely on that).
            # 0 alone is indistinguishable from "no changes", which is
            # why the caller must consult `last_error` before claiming
            # the index is up to date.
            self._last_error = f"{type(e).__name__}: {e}"
            self.record_event(
                "error",
                "Sync could not read Mail (Full Disk Access?)",
                error=self._last_error,
            )
            return 0

        exclude_account_uuids = self._resolve_exclusions()

        result = sync_from_disk(
            self._get_conn(),
            mail_dir,
            progress_callback,
            exclude_account_uuids=exclude_account_uuids,
        )
        # Disk inventory just changed (or was just verified) — drop
        # the get_stats cache so the next status call reflects truth.
        self.invalidate_disk_count_cache()
        self._last_error = None  # a successful sync clears prior failures
        return result.total_changes

    def search(
        self,
        query: str,
        account: str | None = None,
        mailbox: str | None = None,
        limit: int = 20,
        exclude_mailboxes: list[str] | None = None,
        exclude_accounts: list[str] | None = None,
        column: str | None = None,
        *,
        before: str | None = None,
        after: str | None = None,
        offset: int = 0,
        highlight: bool = False,
    ) -> list[SearchResult]:
        """
        Search indexed emails using FTS5.

        Args:
            query: Search query (supports FTS5 syntax)
            account: Optional account filter
            mailbox: Optional mailbox filter
            limit: Maximum results (default: 20)
            exclude_mailboxes: Mailboxes to exclude from results
            column: Optional FTS5 column filter ("subject", "sender",
                or "content")
            before: Exclude emails on/after this date (YYYY-MM-DD)
            after: Include emails on/after this date (YYYY-MM-DD)
            offset: Skip first N results (default: 0)
            highlight: Use FTS5 highlight/snippet for results

        Returns:
            List of SearchResult ordered by relevance (BM25 score)
        """
        from .search import search_fts, search_fts_highlight

        search_fn = search_fts_highlight if highlight else search_fts
        return search_fn(
            self._get_conn(),
            query,
            account=account,
            mailbox=mailbox,
            limit=limit,
            column=column,
            exclude_mailboxes=exclude_mailboxes,
            exclude_accounts=exclude_accounts,
            before=before,
            after=after,
            offset=offset,
        )

    def rebuild(
        self,
        account: str | None = None,
        mailbox: str | None = None,
        progress_callback: Callable[[int, int | None, str], None] | None = None,
    ) -> int:
        """
        Force rebuild of the index.

        Args:
            account: Optional account to rebuild (all if None)
            mailbox: Optional mailbox to rebuild (all in account if None)
            progress_callback: Optional progress callback

        Returns:
            Number of emails re-indexed
        """
        conn = self._get_conn()

        # Delete existing entries for rebuild scope
        if account and mailbox:
            conn.execute(
                "DELETE FROM emails WHERE account = ? AND mailbox = ?",
                (account, mailbox),
            )
        elif account:
            conn.execute("DELETE FROM emails WHERE account = ?", (account,))
        else:
            conn.execute("DELETE FROM emails")

        conn.commit()

        # Rebuild from disk
        return self.build_from_disk(progress_callback)

    def get_indexed_message_ids(
        self, account: str | None = None, mailbox: str | None = None
    ) -> set[int]:
        """
        Get all message IDs currently in the index.

        Note: Message IDs are only unique within (account, mailbox).

        Args:
            account: Optional account filter
            mailbox: Optional mailbox filter

        Returns:
            Set of message IDs
        """
        conn = self._get_conn()

        if account and mailbox:
            sql = """SELECT message_id FROM emails
                     WHERE account = ? AND mailbox = ?"""
            cursor = conn.execute(sql, (account, mailbox))
        elif account:
            cursor = conn.execute(
                "SELECT message_id FROM emails WHERE account = ?", (account,)
            )
        else:
            cursor = conn.execute("SELECT message_id FROM emails")

        return {row[0] for row in cursor}

    # ─────────────────────────────────────────────────────────────────
    # Public Query Methods (used by server.py instead of raw SQL)
    # ─────────────────────────────────────────────────────────────────

    def find_email_location(
        self,
        message_id: int,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> tuple[str, str] | None:
        """Look up an email's (account, mailbox) from the index.

        Used by get_email Strategy 2 to find where an email lives
        without iterating all mailboxes via JXA.

        Args:
            message_id: Mail.app message ID
            account: Optional account filter (UUID)
            mailbox: Optional mailbox filter

        Returns:
            (account, mailbox) tuple or None if not found
        """
        conn = self._get_conn()
        where = ["message_id = ?"]
        params: list = [message_id]
        if account:
            where.append("account = ?")
            params.append(account)
        if mailbox:
            where.append("mailbox = ?")
            params.append(mailbox)

        sql = (
            "SELECT account, mailbox FROM emails WHERE "
            + " AND ".join(where)
            + " LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        if row:
            return (row["account"], row["mailbox"])
        return None

    def get_rfc822_id(
        self,
        message_id: int,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> str | None:
        """Return the stable RFC822 Message-ID for a Mail.app ROWID.

        The ROWID is per-mailbox and changes when a message is filed
        elsewhere — routinely, since most accounts are open on several
        devices. The header survives that, so it is the handle to fall
        back on once a ROWID stops resolving.

        Scope with ``account``/``mailbox`` whenever the caller knows
        them: ``message_id`` is unique only within a mailbox, so an
        unscoped lookup can return a *different* message's header — and
        the caller would then go and modify that unrelated message.

        Returns None when unknown (row absent, or indexed before schema
        v6 and not yet re-indexed).
        """
        conn = self._get_conn()
        where = ["message_id = ?"]
        params: list = [message_id]
        if account:
            where.append("account = ?")
            params.append(account)
        if mailbox:
            where.append("mailbox = ?")
            params.append(mailbox)

        sql = (
            "SELECT rfc822_message_id FROM emails WHERE "
            + " AND ".join(where)
            + " AND rfc822_message_id IS NOT NULL LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        return row["rfc822_message_id"] if row else None

    def count_without_stable_id(self) -> int:
        """Rows lacking an RFC822 Message-ID (indexed before schema v6).

        Those messages cannot be recovered once another device moves
        them, because there is no stable handle on record. A full
        rebuild backfills them.
        """
        try:
            row = (
                self._get_conn()
                .execute(
                    "SELECT COUNT(*) AS n FROM emails "
                    "WHERE rfc822_message_id IS NULL"
                )
                .fetchone()
            )
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0

    def count_skipped_too_large(self) -> int:
        """Messages skipped for exceeding the size ceiling.

        Explains part of any gap between `disk_email_count` and
        `email_count` — a gap that would otherwise look like data loss.
        """
        try:
            row = (
                self._get_conn()
                .execute(
                    "SELECT COUNT(*) AS n FROM failed_index_jobs "
                    "WHERE error_message = 'too_large'"
                )
                .fetchone()
            )
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0

    def find_by_rfc822(
        self, rfc822_message_id: str
    ) -> list[tuple[str, str, int]]:
        """Locate every indexed copy of a message by its stable header.

        A message can legitimately exist more than once (the same mail
        in INBOX and in an archive, or across accounts), so this returns
        all matches rather than guessing. Newest first, so the most
        recently indexed location is tried first.

        Returns:
            List of ``(account, mailbox, message_id)``.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT account, mailbox, message_id FROM emails "
            "WHERE rfc822_message_id = ? ORDER BY indexed_at DESC",
            (rfc822_message_id,),
        ).fetchall()
        return [(r["account"], r["mailbox"], r["message_id"]) for r in rows]

    def find_email_path(
        self,
        message_id: int,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> Path | None:
        """Look up an email's .emlx file path from the index.

        Used by get_attachment to locate the file on disk.

        Args:
            message_id: Mail.app message ID
            account: Optional account filter (UUID)
            mailbox: Optional mailbox filter

        Returns:
            Path to the .emlx file, or None if not found / path is NULL
        """
        conn = self._get_conn()
        where = ["message_id = ?"]
        params: list = [message_id]
        if account:
            where.append("account = ?")
            params.append(account)
        if mailbox:
            where.append("mailbox = ?")
            params.append(mailbox)

        sql = (
            "SELECT emlx_path FROM emails WHERE "
            + " AND ".join(where)
            + " LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        if row and row["emlx_path"]:
            return Path(row["emlx_path"])
        return None

    def delete_email(
        self,
        message_id: int,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> int:
        """Delete a single email entry from the index.

        Used to clean up stale entries when the indexed `.emlx` file
        no longer exists on disk (the message was deleted or moved
        between syncs). The `AFTER DELETE ON emails` trigger handles
        FTS5 cleanup; the `attachments` table cascades via
        `ON DELETE CASCADE`.

        Args:
            message_id: Mail.app message ID
            account: Optional account filter (UUID) — narrows the
                delete when the same message_id appears in multiple
                accounts (rare).
            mailbox: Optional mailbox filter

        Returns:
            Number of rows deleted (typically 0 or 1).
        """
        conn = self._get_conn()
        where = ["message_id = ?"]
        params: list = [message_id]
        if account:
            where.append("account = ?")
            params.append(account)
        if mailbox:
            where.append("mailbox = ?")
            params.append(mailbox)

        sql = "DELETE FROM emails WHERE " + " AND ".join(where)
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    def record_parse_failure(
        self,
        emlx_path: str,
        account: str,
        mailbox: str,
        error: BaseException,
    ) -> None:
        """Record an `.emlx` parse failure into the dead letter queue.

        Idempotent — repeated failures on the same path bump
        `attempt_count` and refresh `last_seen` / `error_*` columns
        without losing `first_seen`.
        """
        from .schema import RECORD_PARSE_FAILURE_SQL, parse_failure_row

        conn = self._get_conn()
        conn.execute(
            RECORD_PARSE_FAILURE_SQL,
            parse_failure_row(emlx_path, account, mailbox, error),
        )
        conn.commit()

    def clear_parse_failure(self, emlx_path: str) -> int:
        """Remove a path from the dead letter queue (e.g. after a
        successful retry). Returns the number of rows removed.
        """
        from .schema import CLEAR_PARSE_FAILURE_SQL

        conn = self._get_conn()
        cursor = conn.execute(CLEAR_PARSE_FAILURE_SQL, (emlx_path,))
        conn.commit()
        return cursor.rowcount

    def search_attachments(
        self,
        query: str,
        account: str | None = None,
        mailbox: str | None = None,
        limit: int = 20,
        exclude_mailboxes: list[str] | None = None,
        exclude_accounts: list[str] | None = None,
        *,
        before: str | None = None,
        after: str | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Search attachments by filename using SQL LIKE.

        Args:
            query: Filename search term (matched with LIKE %query%)
            account: Optional account filter (UUID)
            mailbox: Optional mailbox filter
            limit: Maximum results
            exclude_mailboxes: Mailboxes to exclude from results
            before: Exclude emails on/after this date (YYYY-MM-DD)
            after: Include emails on/after this date (YYYY-MM-DD)
            offset: Skip first N results (default: 0)

        Returns:
            List of dicts with message_id, account, mailbox,
            subject, sender, date_received, filename
        """
        from .search import search_attachments as _search_attachments

        return _search_attachments(
            self._get_conn(),
            query,
            account=account,
            mailbox=mailbox,
            limit=limit,
            exclude_mailboxes=exclude_mailboxes,
            exclude_accounts=exclude_accounts,
            before=before,
            after=after,
            offset=offset,
        )

    def get_email_attachments(
        self,
        message_id: int,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> list[dict] | None:
        """Get attachment metadata for an email from the index.

        Returns richer MIME-parsed attachment data than JXA's
        mailAttachments(), including inline images and S/MIME parts.

        Args:
            message_id: Mail.app message ID
            account: Optional account filter (UUID)
            mailbox: Optional mailbox filter

        Returns:
            List of attachment dicts, or None if email not found
        """
        conn = self._get_conn()
        where = ["e.message_id = ?"]
        params: list = [message_id]
        if account:
            where.append("e.account = ?")
            params.append(account)
        if mailbox:
            where.append("e.mailbox = ?")
            params.append(mailbox)

        sql = (
            "SELECT a.filename, a.mime_type, a.file_size, a.content_id "
            "FROM attachments a "
            "JOIN emails e ON a.email_rowid = e.rowid "
            "WHERE " + " AND ".join(where)
        )
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        if not rows:
            return None
        return [
            {
                "filename": r["filename"],
                "mime_type": r["mime_type"],
                "size": r["file_size"] or 0,
                "content_id": r["content_id"],
            }
            for r in rows
        ]

    # ─────────────────────────────────────────────────────────────────
    # File Watcher Methods
    # ─────────────────────────────────────────────────────────────────

    def start_watcher(
        self,
        on_update: Callable[[int, int], None] | None = None,
    ) -> bool:
        """
        Start the file watcher for real-time index updates.

        Watches ~/Library/Mail/V10/ for .emlx changes and automatically
        updates the index when emails are added or deleted.

        Args:
            on_update: Optional callback(added_count, removed_count)
                       called after each batch of changes

        Returns:
            True if watcher started, False if already running or failed
        """
        if self._watcher is not None and self._watcher.is_running:
            return False

        from .watcher import IndexWatcher

        self._watcher_callback = on_update
        self._watcher = IndexWatcher(
            db_path=self._db_path,
            on_update=on_update,
            exclude_account_uuids=self._resolve_exclusions(),
            write_lock=self._write_lock,
        )

        return self._watcher.start()

    def stop_watcher(self) -> None:
        """Stop the file watcher if running."""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @property
    def watcher_running(self) -> bool:
        """Check if the file watcher is running."""
        return self._watcher is not None and self._watcher.is_running
