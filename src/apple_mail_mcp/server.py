"""
Apple Mail MCP Server

3-layer architecture for fast email access:
1. Disk-first reads — single emails via .emlx parsing (~3ms, no JXA)
2. FTS5 search — full-text body search in ~2ms with BM25 ranking
3. JXA fallback — batch property fetching for multi-email listing

TOOLS (12 total):
- list_accounts() - List email accounts
- list_mailboxes(account?) - List mailboxes
- get_emails(..., filter?) - Unified email listing with filters
- get_email(id) - Get single email with content (disk-first)
- search(query, ...) - Unified search with FTS5 support
- get_email_links(id) - Extract hyperlinks from an email
- get_email_attachment(id, filename) - Extract a file attachment
- get_attachment(id, filename?) - Deprecated alias
- set_flag(ids, color?) - Flag/unflag emails, optionally by color (write)
- set_read_status(ids, read?) - Mark emails read/unread (write)
- get_index_status() - Index health + setup diagnostics
- refresh_index(full?) - Update the search index on demand

RESOURCES (1 total):
- index://status - JSON snapshot of search-index health
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import Literal

# pydantic (via fastmcp tool-schema generation) rejects
# typing.TypedDict on Python < 3.12.
if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from fastmcp import FastMCP

from .builders import (
    FLAG_COLOR_INDEX,
    AccountsQueryBuilder,
    QueryBuilder,
    WriteBuilder,
)
from .config import (
    get_default_account,
    get_default_mailbox,
    get_read_only_mode,
)
from .executor import (
    build_mailbox_setup_js,
    execute_query_async,
    execute_with_core_async,
)

mcp = FastMCP("Apple Mail")

logger = logging.getLogger(__name__)

# Attachment cache directory
ATTACHMENT_CACHE_DIR = _Path.home() / ".apple-mail-mcp" / "attachments"


def _ensure_writable() -> None:
    """Refuse to proceed if the server is running in read-only mode.

    Every MCP tool that mutates Apple Mail state (mark as read, move,
    delete, send, reply, forward, flag, etc.) must call this as its
    first line. A regression test in test_server.py scans this module
    for write-implying tool names and asserts they call this helper.
    """
    if get_read_only_mode():
        raise PermissionError(
            "Server is in read-only mode "
            "(APPLE_MAIL_READ_ONLY=true, [server] read_only = true, "
            "or `apple-mail-mcp serve -r`)."
        )


def _cleanup_old_attachments(max_age_hours: int = 24) -> None:
    """Remove attachment files older than max_age_hours."""
    if not ATTACHMENT_CACHE_DIR.exists():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    for subdir in ATTACHMENT_CACHE_DIR.iterdir():
        if subdir.is_dir():
            try:
                if subdir.stat().st_mtime < cutoff:
                    shutil.rmtree(subdir)
            except OSError:
                pass


def _clamped_env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Read an int env var, clamped to [lo, hi]."""
    return max(lo, min(int(os.environ.get(name, str(default))), hi))


# Strategy 3 safety limits for get_email's all-mailbox scan.
# Clamped so a stray env value can't disable the safety rails.
STRATEGY3_TIMEOUT = _clamped_env_int("APPLE_MAIL_STRATEGY3_TIMEOUT", 15, 1, 300)
STRATEGY3_MAX_MAILBOXES = _clamped_env_int(
    "APPLE_MAIL_STRATEGY3_MAX_MAILBOXES", 50, 1, 500
)

# Hard ceiling for limit params at the MCP tool boundary. Oversized
# limits push entire result sets into the model's context; negative
# LIMIT means "unlimited" in SQLite.
MAX_RESULT_LIMIT = 200

# Hard ceiling for the number of message ids a single write tool call
# may target. Unlike read limits (which clamp), this RAISES: silently
# dropping ids would leave some emails unmodified without the caller
# knowing. One osascript invocation handles the whole batch, so this
# also bounds a single script's work.
MAX_WRITE_BATCH = 500

# The header-recovery scan batch-fetches a Message-ID string per message
# per mailbox, which is heavier than the integer-id scan Strategy 3 does
# — its 15s budget would make recovery time out exactly on the large
# mailboxes it exists for.
# How long refresh_index waits for a spawned build to actually begin
# before answering. Long enough to catch an immediate refusal, short
# enough not to stall the caller.
BUILD_START_TIMEOUT = 5.0

RECOVERY_TIMEOUT = _clamped_env_int("APPLE_MAIL_RECOVERY_TIMEOUT", 60, 5, 300)

# Ceiling for a located (precisely targeted) write. Well under the
# 120s executor default so a wedged Mail.app cannot hold an MCP call
# past most client timeouts.
_TIMEOUT_MARK = "timed out"


def _write_error_text(exc: BaseException) -> str:
    """What to report for a write that did not come back cleanly.

    A timeout is not a failed write — it is an UNKNOWN one: Mail may
    have applied some, all or none of the batch before the deadline.
    Reporting it as "never reached the message" is the same false
    verdict as calling an unfinished search a missing message. It also
    stringifies to nothing, so str(exc) would leave the cause blank.
    """
    if isinstance(exc, TimeoutError):
        return f"Mail {_TIMEOUT_MARK} — outcome unknown"
    return str(exc) or type(exc).__name__


WRITE_TIMEOUT = _clamped_env_int("APPLE_MAIL_WRITE_TIMEOUT", 60, 5, 300)


def _validate_pagination(limit: int, offset: int = 0) -> tuple[int, int]:
    """Clamp pagination params to sane bounds (clamp, don't raise —
    the model's many/few intent survives)."""
    return max(1, min(limit, MAX_RESULT_LIMIT)), max(0, offset)


def _validate_date(value: str | None, param: str) -> str | None:
    """Require YYYY-MM-DD. Malformed dates would otherwise flow into
    SQL string comparisons and silently return wrong results; an
    explicit error lets the model self-correct."""
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        # strptime accepts unpadded "2026-6-1", which still breaks SQL
        # string comparison — require the canonical zero-padded form.
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError(value)
    except ValueError as e:
        raise ValueError(
            f"Invalid {param} date {value!r}: expected YYYY-MM-DD "
            f"(e.g. 2026-01-31)"
        ) from e
    return value


# ========== Response Type Definitions ==========


class Account(TypedDict):
    """An email account in Apple Mail."""

    name: str
    id: str


class Mailbox(TypedDict):
    """A mailbox within an email account."""

    name: str
    unreadCount: int


class EmailSummary(TypedDict, total=False):
    """Summary of an email (used in list/search results)."""

    id: int
    # RFC822 Message-ID header — see SearchResult.message_id. Use this,
    # not `id`, for any follow-up write.
    message_id: str | None
    subject: str
    sender: str
    date_received: str
    read: bool
    flagged: bool
    flag_color: str | None


class SearchResult(TypedDict, total=False):
    """Result from search operations."""

    id: int
    # RFC822 Message-ID header — the stable handle on this message.
    # Prefer it over `id` for any follow-up write: `id` is a per-mailbox
    # ROWID that dies the moment any device files the message elsewhere.
    message_id: str | None
    subject: str
    sender: str
    date_received: str
    score: float
    matched_in: str
    content_snippet: str
    account: str
    mailbox: str


class AttachmentSummary(TypedDict):
    """Summary of an email attachment."""

    filename: str
    mime_type: str
    size: int


class EmailFull(TypedDict, total=False):
    """Complete email with full content."""

    id: int
    subject: str
    sender: str
    content: str
    date_received: str
    date_sent: str
    read: bool
    flagged: bool
    # Which colour the flag has ("red" … "gray"), or None when the
    # message is not flagged. `flagged` alone cannot tell a colour
    # scheme apart, so an audit or migration is blind without it.
    flag_color: str | None
    reply_to: str
    message_id: str
    # Where the message is RIGHT NOW. Addressing a mail by its stable
    # Message-ID makes its current location the one thing the caller
    # cannot work out for itself — and after a move it is the most
    # interesting fact about it.
    account: str
    mailbox: str
    attachments: list[AttachmentSummary]


class WriteResult(TypedDict, total=False):
    """Per-reference outcome of a batch write (set_flag/set_read_status).

    A batch never fails as a whole: every requested reference lands in
    exactly one bucket so partial success is visible to the caller. Each
    is echoed in the form it was passed — an int id as an int, a
    Message-ID header as that header.
    """

    updated: list[int | str]  # actually modified
    unchanged: list[int | str]  # already in the wanted state — no write
    not_found: list[int | str]  # Mail was reachable; the message was not
    failed: list[int | str]  # Mail refused/was unreachable — NOT a verdict
    # Only when something did not land: what the write actually did.
    # Without it a not_found is unfalsifiable from the outside — three
    # separate causes produced the identical empty answer today.
    diagnostics: dict
    error: str  # what Apple Mail actually said, when `failed` is non-empty
    skipped_hidden: list[int | str]  # resolved into an excluded account
    hint: str  # present only when something is actionable (e.g. no index)


# ========== Helper Functions ==========


def _get_index_manager():
    """Get the IndexManager singleton, lazily imported."""
    from .index import IndexManager

    return IndexManager.get_instance()


def _get_account_map():
    """Get the AccountMap singleton, lazily imported."""
    from .index.accounts import AccountMap

    return AccountMap.get_instance()


def _resolve_account(account: str | None) -> str | None:
    """Resolve account, using default from env if not specified."""
    return account if account is not None else get_default_account()


def _resolve_mailbox(mailbox: str | None) -> str:
    """Resolve mailbox, using default from env if not specified."""
    return mailbox if mailbox is not None else get_default_mailbox()


class _AccountHiddenError(ValueError):
    """A message resolved to an excluded account mid-cascade.

    Subclasses ValueError so it reads as a normal "not found" to the
    caller, but the get_email strategy cascade re-raises it past the
    broad ``except Exception`` handlers (which otherwise swallow errors
    to fall through to the next strategy) so a hidden account is never
    fetched live by a later strategy.
    """


def _excluded_account_names() -> set[str]:
    """Account display names hidden from the whole server.

    Configured via ``APPLE_MAIL_INDEX_EXCLUDE_ACCOUNTS`` / ``[index]
    exclude_accounts``. Matched exactly, case-sensitively.
    """
    from .config import get_index_exclude_accounts

    return get_index_exclude_accounts()


async def _excluded_account_uuids() -> set[str]:
    """Resolve hidden account names to UUIDs via the JXA-backed map.

    Empty (and skips the map load) when nothing is configured, so the
    common case adds no overhead. Used to filter the unscoped read
    paths whose rows are keyed by UUID, not by the requested name.
    """
    names = _excluded_account_names()
    if not names:
        return set()
    await _get_account_map().ensure_loaded()
    return _get_account_map().names_to_uuids(names)


def _hidden_account(account: str | None) -> bool:
    """True when the caller explicitly named a hidden account.

    The single audit point for the explicit-account gate every tool
    applies at entry (matching semantics live here: exact,
    case-sensitive display names).
    """
    return account is not None and account in _excluded_account_names()


def _path_in_excluded_account(emlx_path, excluded_uuids: set[str]) -> bool:
    """True when an .emlx path lives under a hidden account's UUID dir.

    Guards stale index rows: an account excluded after indexing (but
    before the next self-healing sync) can still resolve to a path
    under ``V10/<account-uuid>/``. Shared by every disk-read gate so
    the matching rule can't drift between tools.
    """
    return any(u in str(emlx_path) for u in excluded_uuids)


async def _resolve_visible_account(account: str | None) -> str | None:
    """Resolve to a concrete, non-excluded account name for the JXA paths.

    The JXA tools pass ``None`` straight to ``Mail.accounts()[0]`` — so if
    the user gives no account (and no, or an excluded, default), a JXA
    fallback could silently target a hidden account. This resolves ``None``
    (or an excluded default) to the first non-excluded cached account, so a
    hidden account is never the implicit JXA target. Returns ``None`` only
    when there is no visible account at all. Explicit excluded accounts are
    still caught by each tool's early gate before this runs.
    """
    excluded = _excluded_account_names()
    resolved = _resolve_account(account)
    if resolved is not None and resolved not in excluded:
        return resolved
    if not excluded and resolved is None:
        # Nothing hidden and no default — keep the legacy None (JXA
        # picks Mail.accounts()[0]) to avoid a needless JXA map load.
        return None
    await _get_account_map().ensure_loaded()
    for acct in _get_account_map().get_cached_accounts() or []:
        if acct["name"] not in excluded:
            return acct["name"]
    return resolved if not excluded else None


def _detect_matched_columns(query: str, result) -> str:
    """Delegate to search.detect_matched_columns."""
    from .index.search import detect_matched_columns

    return detect_matched_columns(query, result)


async def _overlay_live_flags(result: dict, message_id: int) -> None:
    """Replace disk-derived read/flagged with Mail's live values.

    The `.emlx` plist footer is written when Mail stores the file and is
    not reliably rewritten when the user toggles a flag or read state in
    the UI. Reporting those stale bits makes the assistant contradict
    what the user sees — and skip a write it should have made ("it's
    already flagged"). Apple's Envelope Index has the current values, so
    overlay them in place. Best-effort: on any failure the parsed values
    stand, exactly as before.
    """
    try:
        from .index.disk import find_mail_directory
        from .index.envelope_direct import (
            envelope_index_path,
            fetch_message_flags,
        )

        env_path = envelope_index_path(find_mail_directory())
        if not env_path.exists():
            return
        live = await asyncio.to_thread(
            fetch_message_flags, env_path, message_id
        )
        if live is not None:
            result["read"], result["flagged"] = live
    except Exception as exc:
        logger.debug(
            "Live flag overlay unavailable for %s: %s", message_id, exc
        )


# ========== Diagnostics Helpers ==========


# fork-only:start — the .mcpb bundle tracks a git ref, so the package
# version alone cannot answer "which build is answering me". Upstream
# ships through PyPI, where the package version does answer it.
# Bumped on every shipped change.
SERVER_REVISION = "2026-07-28.19"
# fork-only:end


def to_local_iso(value: str | None) -> str | None:
    """Present a stored timestamp in the viewer's own local time.

    Timestamps are stored in UTC, which is right for storage but wrong
    to hand to a person: a mail Mail.app shows at 14:54 was reported as
    12:54. The conversion uses the running system's timezone via
    ``astimezone()`` — never a fixed zone, since users are not all in
    the same one, and it follows daylight saving automatically.

    Anything unparseable is returned untouched: a listing must never
    break over a cosmetic detail.
    """
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        # Everything this server writes is UTC; say so explicitly
        # rather than letting it be read as local.
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone().isoformat()
    except (OSError, ValueError):
        return value


def _server_version() -> str:
    """Installed package version, or 'unknown' if not resolvable."""
    try:
        from importlib.metadata import version

        return version("apple-mail-mcp")
    except Exception:
        return "unknown"


def get_index_auto_build_flag() -> bool:
    """Read the auto-build setting (thin wrapper for a lazy import)."""
    from .config import get_index_auto_build

    return get_index_auto_build()


def _log_file_facts() -> dict:
    """Does the log exist, and when was it last written?

    Enough to tell "logging works" from "logging is configured and
    silent" without shipping any log CONTENT: the lines carry mail
    subjects and file paths.
    """
    from pathlib import Path as _P

    try:
        path = _P(str(_log_file_path()))
        st = path.stat()
    except (OSError, ValueError):
        return {"log_file_exists": False}
    return {
        "log_file_exists": True,
        "log_file_bytes": st.st_size,
        "log_file_modified": datetime.fromtimestamp(st.st_mtime).isoformat(
            timespec="seconds"
        ),
    }


def _log_file_path() -> str:
    """Where this process writes its log, for the status report."""
    try:
        from .config import get_log_path

        path = get_log_path()
        return str(path) if path is not None else "(disabled)"
    except Exception:
        return "(unknown)"


# fork-only:start — recognises OUR launcher; upstream has no bundle.
def _install_mode() -> str:
    """How this server was launched: 'bundle' (.mcpb) or 'cli'.

    The .mcpb launcher shim exports ``APPLE_MAIL_MCP_LAUNCHER=mcpb``.
    This decides which index command to hand the user: bundle users
    have no ``apple-mail-mcp`` on their PATH, so telling them to run it
    would be a dead end.
    """
    return "bundle" if os.environ.get("APPLE_MAIL_MCP_LAUNCHER") else "cli"


# fork-only:end


def _index_command() -> str:
    """A copy-pasteable command that builds the index for this install."""
    # fork-only:start — bundle installs have no `apple-mail-mcp` on the
    # user's PATH, so the plain command would be a dead end; run the
    # same distribution through `uvx` instead. The launcher passes its
    # own source in APPLE_MAIL_MCP_REF (a git ref for a development
    # build); without it, the published package name is correct.
    # fork-only:end
    # fork-only:start — the bundle route; upstream users have the
    # command on their PATH.
    if _install_mode() == "bundle":
        ref = os.environ.get("APPLE_MAIL_MCP_REF", "").strip()
        source = ref or "apple-mail-mcp"
        return f"uvx --from {source} apple-mail-mcp index --verbose"
    # fork-only:end
    return "apple-mail-mcp index --verbose"


def _index_guidance(
    *,
    state: str,
    mail_dir_accessible: bool,
    mail_dir_missing: bool = False,
    auto_build: bool,
    stalled: bool = False,
    phase: str | None = None,
    syncing: bool = False,
    error: str | None = None,
) -> tuple[str | None, str | None, list[str], str]:
    """Turn raw index state into instructions a non-technical user can follow.

    macOS grants Full Disk Access to the *responsible app* — whichever
    process launches the server — so the fix differs by setup. Returns
    ``(problem, note, next_steps, user_message)``: ``problem`` when
    something needs fixing, ``note`` when the setup is fine but worth
    explaining, and always a plain-language message plus GUI-first
    steps. The terminal command appears only where it is unavoidable.
    """
    cmd = _index_command()
    app = "the app running this"  # fork-only-replace: was a bundle check

    if error and "database is locked" in error.lower():
        return (
            "Another process is holding the index database.",
            None,
            [
                "This is the extension's own database, not Apple Mail's "
                "— quitting Mail does not help and is not needed.",
                "Claude Desktop starts two copies of this server, and "
                "both use the same index. Quit Claude completely "
                "(Cmd-Q), reopen it, and try the rebuild again.",
                "If it repeats, the index is fine — only the rebuild is "
                "blocked. Search, flagging and read/unread keep working.",
            ],
            "Another copy of this extension is holding the index "
            "database, so the rebuild could not run. Restarting Claude "
            "clears it — Apple Mail has nothing to do with it.",
        )
    if syncing:
        return (
            None,
            "An index update is running.",
            [
                "No action needed — counts and the last-sync time only "
                "move when it finishes.",
                "Ask again in a few minutes.",
            ],
            "An index update is running right now; the numbers won't "
            "change until it completes.",
        )
    if state == "building":
        if stalled:
            return (
                "Index build is stuck — nothing written for over two minutes.",
                None,
                [
                    "Quit the app completely (Cmd-Q) and reopen it — that "
                    "ends the stuck build and starts a fresh one.",
                    "If it stalls again, say so: the extension log records "
                    "where the build stopped.",
                ],
                "The index build looks stuck — nothing has been written for "
                "a while. Restarting the app should clear it.",
            )
        if phase == "reading_metadata":
            return (
                None,
                "Index build starting: reading Mail's metadata.",
                [
                    "No action needed. Nothing is written during this "
                    "phase, so a count of zero is expected — on a large "
                    "mailbox it can last several minutes.",
                    "Ask again in a few minutes; the count starts rising "
                    "once indexing begins.",
                ],
                "The index build is in its warm-up phase — it's reading "
                "Mail's metadata before it can write anything, so zero "
                "indexed so far is normal.",
            )
        return (
            None,
            "Index build in progress.",
            [
                "No action needed — the index is building in the background.",
                "Body search will be incomplete until it finishes; "
                "flagging and read/unread already work.",
                "Ask for the index status again in a few minutes.",
            ],
            "I'm still building the mail search index — flagging and "
            "read/unread work already, full-text search will follow "
            "shortly.",
        )

    if mail_dir_accessible:
        if state == "ready":
            return (None, None, [], "The mail index is ready.")
        # Readable Mail but nothing indexed yet.
        if auto_build:
            return (
                "No usable index yet; it builds automatically on server start.",
                None,
                [
                    f"Quit {app} completely (Cmd-Q) and reopen it to "
                    "trigger the build.",
                    "Then ask for the index status again.",
                ],
                "There's no mail search index yet. Restarting the app "
                "will build it automatically.",
            )
        return (
            "No usable index, and automatic building is switched off.",
            None,
            [
                "Either switch 'Build the search index automatically' "
                "back on in the extension's Configure dialog and restart "
                f"{app},",
                f"or open Terminal and run:  {cmd}",
            ],
            "There's no mail search index yet, and automatic building is "
            "turned off — so it has to be built once.",
        )

    # Mail is unreadable from here: this process has no Full Disk Access.
    if state == "ready":
        # Manual setup working exactly as intended.
        return (
            None,
            "Running without Full Disk Access, using an index built elsewhere.",
            [
                "Nothing is broken — search uses the existing index, and "
                "flagging/read-unread work normally.",
                f"To pick up newer mail, run in Terminal:  {cmd}",
            ],
            "Everything works. Search uses the index that was built "
            "outside the app; re-run the index command when you want it "
            "refreshed.",
        )

    if mail_dir_missing:
        # Nothing to read, rather than something we may not read.
        # Diagnosing this as a missing permission sends the user into
        # System Settings to grant access to a directory that does not
        # exist.
        return (
            "Apple Mail has never been set up on this Mac, so there is "
            "no mail to index.",
            None,
            [
                "Open Mail and add an account, then ask again.",
                "If you do use Mail, check that this is the same macOS "
                "user account.",
            ],
            "There is no Apple Mail data on this Mac — Mail has not "
            "been set up here, so there is nothing to index. This is "
            "not a permissions problem.",
        )

    if auto_build:
        return (
            "Cannot read Mail (no Full Disk Access) and there is no index.",
            None,
            [
                "Open System Settings.",
                "Go to Privacy & Security > Full Disk Access.",
                f"Switch on {app} in that list.",
                f"Quit {app} completely (Cmd-Q) and reopen it — the index "
                "then builds itself.",
                "Prefer not to grant that? Instead switch 'Build the "
                "search index automatically' off in the Configure dialog "
                f"and run this in Terminal:  {cmd}",
            ],
            "I can't read your mail archive: this app doesn't have Full "
            "Disk Access, so there's no search index yet. Flagging and "
            "read/unread still work.",
        )

    return (
        "No index, and this app has no Full Disk Access (manual mode).",
        None,
        [
            "Open Terminal.",
            "Give the Terminal app Full Disk Access: System Settings > "
            "Privacy & Security > Full Disk Access.",
            f"Run:  {cmd}",
            "After it finishes, search works here — no permission "
            "needed for this app.",
        ],
        "The index still has to be built once from Terminal — that's the "
        "trade-off for not granting this app full disk access.",
    )


# ========== Write-Tool Helpers ==========


def _header_key(value: str | None) -> str:
    """Comparison key for an RFC822 Message-ID.

    The `.emlx` header carries its angle brackets ("<a@b>"); Apple
    Mail's `messageId` property hands back the bare addr-spec ("a@b").
    Both name the same message, and a strict comparison between them
    never matches — which is exactly how every Message-ID lookup came
    back "not found" while the message was sitting in the mailbox.
    Compare through this, never on the raw strings.
    """
    if not value:
        return ""
    key = str(value).strip()
    if key.startswith("<") and key.endswith(">"):
        key = key[1:-1]
    return key


MessageRef = int | str


def _normalize_message_ids(
    message_ids: MessageRef | list[MessageRef],
) -> list[MessageRef]:
    """Coerce a single reference or list into a validated, unique list.

    A reference is either a Mail.app id (int) or an RFC822 Message-ID
    header (str, e.g. ``"<abc@example.com>"``). The header is the stable
    one: an int stops resolving the moment any device files the message
    elsewhere, so anything held across calls should be a header.

    Rejects bools (``True``/``False`` are not ids even though ``bool``
    is an ``int`` subclass) and blank strings. Raises on an empty batch
    or one larger than :data:`MAX_WRITE_BATCH` — a write must never
    silently drop targets. Order is preserved; duplicates collapse.
    """
    if isinstance(message_ids, bool):
        raise ValueError("message_ids must not be a bool.")
    if isinstance(message_ids, (int, str)):
        raw: list = [message_ids]
    elif isinstance(message_ids, (list, tuple)):
        raw = list(message_ids)
    else:
        raise ValueError(
            f"message_ids must be an id, a Message-ID header, or a list "
            f"of them; got {type(message_ids).__name__}."
        )

    ids: list[MessageRef] = []
    seen: set[MessageRef] = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError(
                f"message reference must be an int id or a Message-ID "
                f"string, got {item!r} ({type(item).__name__})."
            )
        if isinstance(item, str):
            item = item.strip()
            if not item:
                raise ValueError(
                    "message reference must not be an empty string."
                )
            # HTML-escaped brackets are another shape the same
            # reference arrives in: "&lt;a@b&gt;", sometimes wrapping a
            # stringified list as well. Nothing in it trips the checks
            # below, so it would sail through and miss in silence —
            # observed live, where the caller happened to notice.
            # Unescape FIRST: an escaped list has to become a list
            # before it can be unwrapped as one.
            #
            # Only the three entities that carry STRUCTURE are decoded.
            # `&amp;` is deliberately left alone: `&` is legal in a
            # Message-ID local part, so "<a&amp;b@x>" and "<a&b@x>" can
            # both exist as distinct messages, and decoding would aim
            # the write at the other one while reporting success. A
            # reference that then fails to match is answered with a
            # miss, which is recoverable; a write to the wrong message
            # is not.
            if "&lt;" in item or "&gt;" in item or "&quot;" in item:
                item = (
                    item.replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .strip()
                )
            # Some clients serialize a list parameter as JSON text, so
            # what arrives is the string '["<a@b>"]' rather than a list.
            # Taken literally that is a Message-ID nothing will ever
            # match, and the caller gets a mute not_found for a message
            # sitting right there. Unwrap it instead.
            if item.startswith("[") and item.endswith("]"):
                try:
                    unwrapped = json.loads(item)
                except ValueError:
                    unwrapped = None
                if isinstance(unwrapped, list):
                    if not unwrapped:
                        raise ValueError(
                            "message_ids is empty; provide at least one "
                            "reference."
                        )
                    raw.extend(unwrapped)
                    continue
            # A stray pair of quotes around the value is the same class
            # of accident.
            if len(item) > 1 and item[0] == item[-1] and item[0] in "\"'":
                item = item[1:-1].strip()
            if any(c in item for c in '"\n\r\t') or " " in item:
                raise ValueError(
                    f"{item!r} is not a usable message reference: a "
                    f"Message-ID contains no quotes or whitespace. Pass "
                    f"the `message_id` field from a search or get_emails "
                    f"result, or a list of them."
                )
            # Last, once the string is clean: an all-digit reference is
            # the numeric ID, not a Message-ID. MCP clients stringify
            # numbers routinely, and reading "150540" as a header sent
            # it down the recovery path — a scan of EVERY visible
            # account for a header nothing can hold. A real Message-ID
            # is an addr-spec and carries an "@".
            if item.isdigit():
                item = int(item)
        # Keyed through _header_key: "<a@b>" and "a@b" are the SAME
        # message, and the brackets are not part of the identity. Both
        # spellings in one batch used to be written twice.
        key = _header_key(item) if isinstance(item, str) else item
        if key not in seen:
            seen.add(key)
            ids.append(item)

    if not ids:
        raise ValueError(
            "message_ids is empty; provide at least one reference."
        )
    if len(ids) > MAX_WRITE_BATCH:
        raise ValueError(
            f"Too many ids ({len(ids)}); max {MAX_WRITE_BATCH} per call. "
            f"Split into smaller batches."
        )
    return ids


async def _visible_account_names() -> list[str]:
    """Every account the caller is allowed to see, in Mail's order.

    A header the index cannot place used to be looked for in ONE
    account — the default. A message that had simply not been indexed
    yet (it arrived after the last sync) was therefore unreachable in
    any other account, and reported as a mute not_found. Measured: the
    NEWEST message in a mailbox failed while a 2013 one succeeded.
    """
    acct_map = _get_account_map()
    excluded = _excluded_account_names()
    cached = acct_map.get_cached_accounts()
    if cached is None:
        try:
            await acct_map.ensure_loaded()
            cached = acct_map.get_cached_accounts()
        except Exception as exc:
            logger.debug("account enumeration failed: %s", exc)
            cached = None
    names = [
        a.get("name")
        for a in (cached or [])
        if a.get("name") and a.get("name") not in excluded
    ]
    return names


async def _overlay_flag_color(
    result: dict, message_id: int, account: str | None, mailbox: str | None
) -> None:
    """Fetch the flag colour for a message the disk path served.

    The `.emlx` footer bitmask gives read and flagged; the COLOUR is not
    documented there, and guessing at bit positions would invent flags.
    Apple reports it directly as `flagIndex`, so ask for that one
    property for this one message — but only when the message is
    actually flagged, so an unflagged listing costs nothing.
    """
    if not result.get("flagged") or result.get("flag_color"):
        return
    if not account or not mailbox:
        return
    script = f"""
const account = MailCore.getAccount({json.dumps(account)});
const mailbox = MailCore.getMailbox(account, {json.dumps(mailbox)});
const ids = mailbox.messages.id();
const idx = ids.indexOf({int(message_id)});
JSON.stringify({{
    flag_color: idx === -1
        ? null
        : MailCore.flagColorName(mailbox.messages[idx].flagIndex()),
}});
"""
    try:
        got = await execute_with_core_async(script, timeout=STRATEGY3_TIMEOUT)
        if got and got.get("flag_color"):
            result["flag_color"] = got["flag_color"]
    except Exception as exc:
        logger.debug("flag colour unavailable for %s: %s", message_id, exc)


async def _overlay_flag_colors_bulk(
    rows: list[dict], account: str, mailbox: str
) -> None:
    """Resolve the flag colour for a whole page in ONE osascript call.

    Asking per message costs a process spawn each: a survey of 57
    flagged messages took a minute. Apple hands out a property for the
    entire mailbox in a single bulk fetch, so the cost is two of those
    regardless of how many messages the page holds.
    """
    wanted = [r["id"] for r in rows if r.get("flagged") and r.get("id")]
    if not wanted or not account or not mailbox:
        return
    script = f"""
const account = MailCore.getAccount({json.dumps(account)});
const mailbox = MailCore.getMailbox(account, {json.dumps(mailbox)});
const ids = mailbox.messages.id();
const flags = mailbox.messages.flagIndex();
const out = {{}};
for (const id of {json.dumps(wanted)}) {{
    const i = ids.indexOf(id);
    out[String(id)] = i === -1 ? null : MailCore.flagColorName(flags[i]);
}}
JSON.stringify(out);
"""
    try:
        colors = await execute_with_core_async(
            script, timeout=STRATEGY3_TIMEOUT
        )
    except Exception as exc:
        logger.debug("bulk flag colours unavailable: %s", exc)
        return
    if not isinstance(colors, dict):
        return
    for row in rows:
        color = colors.get(str(row.get("id")))
        if color:
            row["flag_color"] = color


def _parse_date_bound(value: str | None, name: str) -> float | None:
    """ISO date/datetime -> Unix timestamp for the Envelope Index.

    Naive input is read as local time, because that is what a caller
    typing a date means; the column stores Unix epoch.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"`{name}` must be an ISO date or datetime "
            f"(2026-07-28 or 2026-07-28T09:30), got {value!r}."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


async def _resolve_write_targets(
    ids: list[MessageRef],
    account: str | None,
    mailbox: str | None,
) -> tuple[
    list[dict],
    list[MessageRef],
    list[MessageRef],
    dict[int, tuple[str, str]],
    list[MessageRef],
]:
    """Resolve message ids to JXA write groups, honoring the account gate.

    Reuses the index's location resolver (the same machinery ``get_email``
    leans on) to place each id in its ``(account, mailbox)``, since Mail.app
    ids are per-mailbox ROWIDs and not globally addressable. An id that
    resolves into an excluded account (#90) is dropped into
    ``skipped_hidden`` and never dispatched to JXA. Ids the index can't
    place fall back to an explicit ``account`` + ``mailbox`` hint (which
    JXA then verifies) — matching ``get_email`` Strategy 1 — and, failing
    that, to a bounded all-mailbox **scan** of a visible account (a
    ``{"account", "ids", "scan": True}`` group, mirroring ``get_email``
    Strategy 3) so writes work with no index at all. Only ids with no
    visible account to scan land in ``not_found``.

    Returns ``(groups, not_found, skipped_hidden, placed)`` where
    ``placed`` maps each id to the ``(account_uuid, mailbox)`` the index
    resolved it to. Recovery needs that scope: a Mail.app id is unique
    only within a mailbox, so looking its stable header up unscoped can
    return a different message's header entirely.

    References given as RFC822 Message-ID headers take a different route
    entirely: they are never translated back into a ROWID. The index is
    consulted only to pick the account and to order the mailbox scan
    (``prefer_mailboxes``); the write itself matches on
    ``msg.messageId()`` in JXA, so a stale index row can misdirect the
    scan but can never cause the wrong message to be written. When the
    index has no row at all, the header is scanned for in the hinted or
    default visible account.

    Located groups are ``{"account", "mailbox", "ids"}``; the optional
    scan group is ``{"account", "ids", "scan": True}``; header groups are
    ``{"account", "headers", "prefer_mailboxes", "by_header": True}``.
    """
    # Explicit hidden account: refuse the whole batch up front, exactly
    # as the read tools do at their entry gate.
    if _hidden_account(account):
        return [], [], list(ids), {}, []

    manager = _get_index_manager()
    has_index = manager.has_index()

    acct_map = _get_account_map()
    excluded_names = _excluded_account_names()
    excluded_uuids: set[str] = set()
    idx_acct_uuid: str | None = None

    if has_index or excluded_names or account:
        await acct_map.ensure_loaded()
        excluded_uuids = acct_map.names_to_uuids(excluded_names)
        if account:
            idx_acct_uuid = acct_map.name_to_uuid(account)

    # Fallback target for ids the index can't place: only usable when the
    # caller pinned BOTH account and mailbox (the account is known
    # non-hidden here — the explicit-hidden case returned above).
    hint_location: tuple[str, str] | None = (
        (account, mailbox) if account and mailbox else None
    )

    grouped: dict[tuple[str, str], list[int]] = {}
    scan_ids: list[int] = []
    not_found: list[MessageRef] = []
    skipped_hidden: list[MessageRef] = []
    placed: dict[int, tuple[str, str]] = {}
    # account name -> {"headers": [...], "prefer": {mailbox, ...}}
    header_groups: dict[str | None, dict] = {}

    int_ids = [m for m in ids if isinstance(m, int)]
    headers = [m for m in ids if isinstance(m, str)]

    for header in headers:
        matches = (
            await asyncio.to_thread(manager.find_by_rfc822, header)
            if has_index
            else []
        )
        if account:
            # An explicit account both scopes and disambiguates.
            matches = [
                m
                for m in matches
                if idx_acct_uuid is not None and m[0] == idx_acct_uuid
            ]
        visible = [m for m in matches if m[0] not in excluded_uuids]
        if matches and not visible:
            # Every known copy lives in a hidden account (#90).
            skipped_hidden.append(header)
            continue

        if visible:
            # find_by_rfc822 returns newest-indexed first. A header found
            # in two accounts (a forward, a list subscribed twice) is
            # written in the most recently indexed one; pass `account` to
            # pin it. Mailboxes of that account seed the scan order.
            target_acct = acct_map.uuid_to_name(visible[0][0])
            prefer = {mb for acct, mb, _ in visible if acct == visible[0][0]}
            # The index says where it WAS. Treat that as the first
            # place to look, not as the only one: a row can be stale,
            # and a miss there used to end the search in silence while
            # the message sat in another account. Later accounts cost
            # nothing once the header has been settled.
            others = [
                a
                for a in await _visible_account_names()
                if a and a != target_acct
            ]
            targets = [target_acct, *others]
        elif account:
            # Caller pinned the account: honour it, scan nothing else.
            targets = [await _resolve_visible_account(account)]
            if excluded_names and (
                targets[0] is None or targets[0] in excluded_names
            ):
                not_found.append(header)
                continue
            prefer = {mailbox} if mailbox else set()
        else:
            # The index cannot place it — most often because it arrived
            # after the last sync. Search EVERY visible account: with
            # one account only, a message outside it is unreachable and
            # comes back as a mute "not found".
            targets = await _visible_account_names()
            if not targets:
                targets = [await _resolve_visible_account(None)]
            if excluded_names and not [
                t for t in targets if t and t not in excluded_names
            ]:
                not_found.append(header)
                continue
            prefer = {mailbox} if mailbox else set()

        for target_acct in targets:
            entry = header_groups.setdefault(
                target_acct, {"headers": [], "prefer": set()}
            )
            entry["headers"].append(header)
            entry["prefer"] |= prefer

    ambiguous: list[MessageRef] = []
    for mid in int_ids:
        located: tuple[str, str] | None = None
        if has_index:
            # A Mail.app id is a per-mailbox ROWID, so the index can
            # legitimately hold several rows for the same number. Taking
            # the first would write to a message the caller never named
            # — for a WRITE that is the worst possible guess. The
            # mailbox is part of the question, not an answer to it:
            # naming "INBOX" without an account still leaves every
            # account's INBOX, and the same ROWID names a different
            # message in each.
            if (
                manager.count_email_locations(
                    mid, account=idx_acct_uuid, mailbox=mailbox
                )
                > 1
            ):
                ambiguous.append(mid)
                continue
            loc = manager.find_email_location(
                mid, account=idx_acct_uuid, mailbox=mailbox
            )
            if loc:
                acct_uuid, mb_name = loc
                if acct_uuid in excluded_uuids:
                    skipped_hidden.append(mid)
                    continue
                located = (acct_map.uuid_to_name(acct_uuid), mb_name)
                placed[mid] = (acct_uuid, mb_name)
        if located is None and hint_location is not None:
            located = hint_location
        if located is None:
            # No index hit, no hint: defer to a bounded JXA scan below.
            scan_ids.append(mid)
            continue
        grouped.setdefault(located, []).append(mid)

    groups: list[dict] = [
        {"account": acct, "mailbox": mb, "ids": mids}
        for (acct, mb), mids in grouped.items()
    ]
    groups += [
        {
            "account": acct,
            "headers": entry["headers"],
            "prefer_mailboxes": sorted(entry["prefer"]),
            "by_header": True,
        }
        # Insertion order, NOT alphabetical: the account the index
        # points at was inserted first and has to be searched first.
        # Sorting by name threw that priority away.
        for acct, entry in header_groups.items()
    ]

    # Index-free / index-miss fallback: scan one visible account's
    # mailboxes for the ids we couldn't place. Bounded (mailbox cap in the
    # builder, timeout at the call site). Only one account is scanned —
    # the same single-account limitation as get_email's Strategy 3.
    if scan_ids:
        scan_account = await _resolve_visible_account(account)
        # A None account is legitimate: MailCore.getAccount(null) picks
        # the first account, which is the documented default. Only bail
        # when exclusions are active and nothing visible remains.
        if excluded_names and (
            scan_account is None or scan_account in excluded_names
        ):
            not_found.extend(scan_ids)
        else:
            groups.append(
                {"account": scan_account, "ids": scan_ids, "scan": True}
            )

    return groups, not_found, skipped_hidden, placed, ambiguous


def _absorb_failures(res: dict, failed: list, errors: list, as_int: bool):
    """Move JXA-reported failures out of the caller's success path.

    The write script distinguishes "Mail was open and the message was
    not there" (``not_found``) from "we never got that far" (
    ``failures``, each with its reason). Merging the two is what made a
    broken account or mailbox lookup look like a batch of deleted mail.
    """
    for item in res.get("failures", []) or []:
        target = item.get("target")
        if target is None:
            continue
        failed.append(int(target) if as_int else str(target))
        reason = str(item.get("reason", "")).strip()
        if reason:
            errors.append(reason)


async def _apply_write(
    message_ids: MessageRef | list[MessageRef],
    account: str | None,
    mailbox: str | None,
    make_builder,
) -> WriteResult:
    """Shared orchestration for the batch write tools.

    Normalizes references, resolves targets (with the account gate), then
    runs the located groups, any scan group and any Message-ID group in
    *separate* osascript calls — so a slow/timed-out mailbox scan can't
    discard the fast, precise located writes — and merges every
    reference's outcome. ``make_builder`` maps ``groups -> WriteBuilder``
    (the only per-tool difference).

    Every reference comes back in exactly one bucket, reported in the
    same form the caller passed it: ints as ints, Message-ID headers as
    headers.
    """
    ids = _normalize_message_ids(message_ids)
    (
        groups,
        not_found,
        skipped_hidden,
        placed,
        ambiguous,
    ) = await _resolve_write_targets(ids, account, mailbox)

    located = [
        g for g in groups if not g.get("scan") and not g.get("by_header")
    ]
    scan = [g for g in groups if g.get("scan")]
    by_header = [g for g in groups if g.get("by_header")]
    updated: list[MessageRef] = []
    unchanged: list[MessageRef] = []
    # A write that never reached Apple Mail is NOT evidence that the
    # message is gone. Reporting it as not_found sent the caller hunting
    # for a message that was there all along.
    failed: list[MessageRef] = []
    errors: list[str] = []
    # Mailboxes the scan never reached: a miss that follows one of
    # these is not evidence that the message is gone.
    unsearched = 0

    if located:
        try:
            res = await execute_with_core_async(
                make_builder(located).build(), timeout=WRITE_TIMEOUT
            )
            updated += [int(x) for x in res.get("updated", [])]
            unchanged += [int(x) for x in res.get("unchanged", [])]
            not_found += [int(x) for x in res.get("not_found", [])]
            _absorb_failures(res, failed, errors, as_int=True)
            unsearched += (
                int(res.get("scan_capped") or 0)
                + int(res.get("scan_unreadable") or 0)
                + int(res.get("scan_skipped_discard") or 0)
            )
        except Exception as exc:
            # The contract is that every id lands in exactly one bucket.
            # Letting this raise would leave the caller with no idea
            # which writes did or did not happen.
            logger.warning("located write failed: %s", exc, exc_info=True)
            failed += [i for g in located for i in g["ids"]]
            errors.append(_write_error_text(exc))

    if scan:
        builder = make_builder(scan)
        builder.max_scan_mailboxes = STRATEGY3_MAX_MAILBOXES
        try:
            res = await execute_with_core_async(
                builder.build(), timeout=STRATEGY3_TIMEOUT
            )
            updated += [int(x) for x in res.get("updated", [])]
            unchanged += [int(x) for x in res.get("unchanged", [])]
            not_found += [int(x) for x in res.get("not_found", [])]
            _absorb_failures(res, failed, errors, as_int=True)
            unsearched += (
                int(res.get("scan_capped") or 0)
                + int(res.get("scan_unreadable") or 0)
                + int(res.get("scan_skipped_discard") or 0)
            )
        except Exception as exc:
            # Best-effort fallback: a timed-out or failed scan reports its
            # ids as not_found rather than erroring the whole call.
            logger.warning("write scan failed: %s", exc, exc_info=True)
            failed += [i for g in scan for i in g["ids"]]
            errors.append(_write_error_text(exc))

    if by_header:
        # Caller-supplied Message-IDs. Same bounded scan as the recovery
        # path — and the same timeout, since it walks mailboxes rather
        # than addressing a message directly.
        builder = make_builder(by_header)
        builder.max_scan_mailboxes = STRATEGY3_MAX_MAILBOXES
        try:
            res = await execute_with_core_async(
                builder.build(), timeout=RECOVERY_TIMEOUT
            )
            updated += [str(x) for x in res.get("updated", [])]
            unchanged += [str(x) for x in res.get("unchanged", [])]
            not_found += [str(x) for x in res.get("not_found", [])]
            _absorb_failures(res, failed, errors, as_int=False)
            unsearched += (
                int(res.get("scan_capped") or 0)
                + int(res.get("scan_unreadable") or 0)
                + int(res.get("scan_skipped_discard") or 0)
            )
        except Exception as exc:
            logger.warning("Message-ID write failed: %s", exc, exc_info=True)
            failed += [h for g in by_header for h in g["headers"]]
            errors.append(_write_error_text(exc))

    # Recovery: a ROWID stops resolving as soon as any device files the
    # message elsewhere — the normal case with phones and tablets on the
    # same account. Re-find those by their RFC822 Message-ID, which
    # survives moves, and apply there.
    dead_ids = [m for m in not_found if isinstance(m, int)]
    if dead_ids:
        (
            recovered,
            still_missing,
            moved,
            recovery_gaps,
        ) = await _retry_by_stable_id(dead_ids, account, make_builder, placed)
        unsearched += recovery_gaps
        updated += recovered["updated"]
        unchanged += recovered["unchanged"]
        # Headers that failed have already been matched on their stable
        # id — there is nothing left to recover them by.
        not_found = [
            m for m in not_found if not isinstance(m, int)
        ] + still_missing
    else:
        moved = []

    result: WriteResult = {
        "updated": updated,
        "unchanged": unchanged,
        "not_found": not_found,
        "skipped_hidden": skipped_hidden,
    }
    if not_found or failed:
        # Say what was actually attempted. A bare not_found cannot be
        # checked by the caller: it looks the same whether the index
        # placed the message, which accounts were searched, or whether
        # the reference arrived in the shape the caller intended.
        header_groups = [g for g in by_header]
        result["diagnostics"] = {
            "accounts_searched": [g.get("account") for g in header_groups],
            "mailboxes_preferred": sorted(
                {
                    mb
                    for g in header_groups
                    for mb in (g.get("prefer_mailboxes") or [])
                }
            ),
            "located_by_index": [
                f"{acct}/{mbox}" for acct, mbox in placed.values()
            ],
            "references_as_received": [str(i) for i in ids],
            "mailboxes_not_searched": unsearched,
        }
    if ambiguous:
        # Never guessed at: the same number names a different message in
        # another mailbox, and picking one would write to mail the
        # caller did not ask about.
        failed.extend(ambiguous)
        errors.append(f"{len(ambiguous)} id(s) exist in more than one mailbox")
        result["failed"] = failed
        result["error"] = "; ".join(dict.fromkeys(errors))[:500]
        result["hint"] = (
            f"{len(ambiguous)} id(s) name a message in more than one "
            f"mailbox — a Mail.app id is only unique within its mailbox, "
            f"so the number alone does not say which message you mean. "
            f"Pass `mailbox` (and `account`) to say which one, and "
            f"nothing was written for them."
        )
        return result

    if failed:
        # Say plainly that Apple Mail never carried the write out, and
        # what it said — the caller must not read this as "deleted".
        result["failed"] = failed
        result["error"] = "; ".join(dict.fromkeys(errors))[:500]
        blob = result["error"].lower()
        if "no such account" in blob:
            cause = (
                "The account name taken from the index does not match any "
                "account in Mail. Call refresh_index() and retry; passing "
                "`account` with the name Mail shows also works."
            )
        elif "cannot open mailbox" in blob or "cannot list mailboxes" in blob:
            cause = (
                "The mailbox could not be opened under that name — the "
                "index may name it differently than Mail does. Retry with "
                "the Message-ID instead of the numeric id, which searches "
                "by header rather than by mailbox name."
            )
        elif "-1743" in blob or "not authorized" in blob:
            cause = (
                "This process has no Automation permission for Mail "
                "(System Settings > Privacy & Security > Automation). A "
                "background or scheduled run cannot show that consent "
                "dialog — it has to be granted once interactively."
            )
        else:
            cause = (
                "Check that Mail.app is running and that this process may "
                "control it (System Settings > Privacy & Security > "
                "Automation)."
            )
        if _TIMEOUT_MARK in blob:
            # A timeout says the ANSWER never came back, not that the
            # write never happened: Mail may have applied some, all or
            # none of them before the deadline.
            result["hint"] = (
                f"{len(failed)} write(s) timed out: Mail did not answer "
                f"in time, so whether it applied them is UNKNOWN — some, "
                f"all or none may have gone through. Read the messages "
                f"back to see the current state before retrying; "
                f"re-running is safe, because setting a flag or a read "
                f"status twice changes nothing. Reported: "
                f"{result['error']}"
            )
            return result
        result["hint"] = (
            f"{len(failed)} write(s) never reached the message — this is "
            f"NOT a statement about the mail, which is most likely fine. "
            f"Reported: {result['error']} — {cause} Reads keep working "
            f"meanwhile because they come from the index and the .emlx "
            f"files, not from Apple Events."
        )
        return result
    missing_headers = [m for m in not_found if isinstance(m, str)]
    if missing_headers and not [m for m in not_found if isinstance(m, int)]:
        # A Message-ID write was matched against the live mailboxes, so
        # a miss is a statement about Apple Mail, not about the index.
        if unsearched:
            result["hint"] = (
                f"{len(missing_headers)} message(s) were not found, but "
                f"{unsearched} mailbox(es) were never searched (scan "
                f"limit, unreadable, or a trash/junk mailbox that is "
                f"skipped unless the index expects the message there). "
                f"This is "
                f"NOT evidence that the message is gone — pass `account` "
                f"and `mailbox` to aim the write, or call "
                f"refresh_index() so the index can place it directly."
            )
        else:
            result["hint"] = (
                f"{len(missing_headers)} message(s) with that Message-ID "
                f"were not found in any visible account. Apple Mail was "
                f"reachable and every mailbox was searched, so the "
                f"message is most likely deleted."
            )
    elif moved:
        result["hint"] = (
            f"{len(moved)} message(s) had been moved (another device, or a "
            f"mail rule) and were re-found by their Message-ID header. "
            f"Their ids have changed; call refresh_index() to update them."
        )
    elif not_found and unsearched:
        # Covers the numeric ids too: recovery may have timed out or
        # skipped mailboxes, and that gap was recorded but never read
        # here — so a moved message came back as "probably deleted".
        result["hint"] = (
            f"{len(not_found)} reference(s) were not found, but "
            f"{unsearched} place(s) were never searched (scan limit, an "
            f"unreadable mailbox, a skipped trash/junk mailbox, or a "
            f"recovery that did not finish). This is NOT evidence that "
            f"the messages are gone — pass `account` and `mailbox`, or "
            f"call refresh_index() and retry."
        )
    elif not_found and not _get_index_manager().has_index():
        result["hint"] = (
            "Some ids weren't found by scanning the default account. Pass "
            "both account and mailbox for reliable resolution, or call "
            "get_index_status() — building the index makes id lookup "
            "exact, and it will explain how."
        )
    elif not_found:
        # No claim of deletion here: a numeric id is a per-mailbox
        # ROWID, so this search covered one account and the mailboxes
        # the index pointed at — never "everywhere". Say what that
        # means and name the handle that does span accounts.
        result["hint"] = (
            "Some ids could not be located. A numeric id is only unique "
            "within a mailbox, so this searched one account and the "
            "places the index knows — not every account. No stable "
            "Message-ID was on record to widen the search with: call "
            "refresh_index(full=True) to record them, pass `account` "
            "and `mailbox`, or address the messages by their "
            "`message_id`, which is searched across all accounts."
        )
    return result


async def _retry_by_stable_id(
    missing: list[int],
    account: str | None,
    make_builder,
    placed: dict[int, tuple[str, str]],
) -> tuple[dict[str, list[int]], list[int], list[int], int]:
    """Re-apply a failed write using the RFC822 Message-ID.

    Mail.app ids are per-mailbox ROWIDs: the moment another device (or a
    server-side rule) files a message elsewhere, the id we hold is dead
    even though the message is perfectly fine. The header is stable, so
    look it up in the index, scan for it, and write there.

    Returns ``(results, still_missing, moved, unsearched)``. ``results``
    has ``updated`` / ``unchanged`` keyed back to the *original* ids,
    ``still_missing`` are ids with no stable id or no match, ``moved``
    are the ids recovered elsewhere, and ``unsearched`` counts the
    mailboxes this recovery never looked in — a cap, an unreadable
    mailbox, a skipped discard mailbox, or an outright failure. Without
    that count the caller would present an unfinished recovery as proof
    the message is gone.
    """
    empty: dict[str, list[int]] = {"updated": [], "unchanged": []}
    manager = _get_index_manager()
    if not manager.has_index():
        return empty, missing, [], 0

    # Map each dead id to its stable header. SCOPE the lookup to where
    # the index placed that id: a Mail.app id is unique only within a
    # mailbox, so an unscoped lookup can return a different message's
    # header — and we would then modify that unrelated message.
    header_by_id: dict[int, str] = {}
    for mid in missing:
        scope = placed.get(mid)
        if scope is None:
            # Never resolved to a location, so there is nothing to scope
            # by and no way to tell copies apart. Leave it missing.
            continue
        acct_uuid, mbox = scope
        header = await asyncio.to_thread(
            manager.get_rfc822_id, mid, acct_uuid, mbox
        )
        if header:
            header_by_id[mid] = header
    if not header_by_id:
        return empty, missing, [], 0

    # One header may belong to several requested ids (the same mail
    # filed in two mailboxes). The scan applies to ONE copy per header,
    # so crediting every id sharing it would report writes that never
    # happened. Recover only unambiguous headers.
    ids_per_header: dict[str, list[int]] = {}
    for mid, header in header_by_id.items():
        ids_per_header.setdefault(header, []).append(mid)
    unambiguous = {
        h: ids[0] for h, ids in ids_per_header.items() if len(ids) == 1
    }
    if not unambiguous:
        return empty, missing, [], 0

    # Scan the account the message actually lived in — NOT the default
    # one. Aiming every recovery at `Mail.accounts()[0]` would make it
    # useless for anyone whose mail is not in the first account, and
    # worse: with the same Message-ID present in two accounts (a
    # forward, a list subscribed twice), preferring a same-named
    # mailbox there would flag the wrong account's copy and report it
    # as done.
    acct_map = _get_account_map()
    excluded = _excluded_account_names()
    by_account: dict[str, dict[str, list]] = {}
    for header, mid in unambiguous.items():
        scope = placed.get(mid)
        if scope is None:
            continue
        acct_name = acct_map.uuid_to_name(scope[0])
        if acct_name in excluded:
            continue
        entry = by_account.setdefault(
            acct_name, {"headers": [], "prefer": set()}
        )
        entry["headers"].append(header)
        entry["prefer"].add(scope[1])
    if not by_account:
        return empty, missing, [], 0

    builder = make_builder(
        [
            {
                "account": acct_name,
                "headers": sorted(entry["headers"]),
                "prefer_mailboxes": sorted(entry["prefer"]),
                "by_header": True,
            }
            for acct_name, entry in sorted(by_account.items())
        ]
    )
    builder.max_scan_mailboxes = STRATEGY3_MAX_MAILBOXES
    try:
        res = await execute_with_core_async(
            builder.build(), timeout=RECOVERY_TIMEOUT
        )
    except Exception as exc:
        logger.warning("stable-id recovery failed: %s", exc, exc_info=True)
        # The recovery never ran. Flag it as unsearched so the caller
        # cannot read the outcome as "the message is not there".
        return empty, missing, [], 1

    # JXA answers in headers; map each back to its single id.
    results: dict[str, list[int]] = {"updated": [], "unchanged": []}
    for bucket in ("updated", "unchanged"):
        for header in res.get(bucket, []):
            mid = unambiguous.get(header)
            if mid is not None:
                results[bucket].append(mid)

    recovered = set(results["updated"]) | set(results["unchanged"])
    still_missing = [m for m in missing if m not in recovered]
    unsearched = (
        int(res.get("scan_capped") or 0)
        + int(res.get("scan_unreadable") or 0)
        + int(res.get("scan_skipped_discard") or 0)
        + len(res.get("failures") or [])
    )
    return results, still_missing, sorted(recovered), unsearched


# ========== MCP Tools (10 total) ==========


@mcp.tool
async def list_accounts() -> list[Account]:
    """
    List all configured email accounts in Apple Mail.

    Returns:
        List of account dictionaries with 'name' and 'id' fields.

    Example:
        >>> list_accounts()
        [{"name": "Work", "id": "abc123"}, {"name": "Personal", "id": "def456"}]
    """
    # Strategy 0: serve from the AccountMap cache when it's warm.
    # The cache is hydrated by any prior list_accounts() call (5-min
    # TTL) — so within a single MCP session this skips the ~150ms
    # JXA round-trip on repeat calls.
    excluded = _excluded_account_names()
    cached = _get_account_map().get_cached_accounts()
    if cached is not None:
        visible = [a for a in cached if a.get("name") not in excluded]
        return visible  # type: ignore[return-value]

    # Strategy 1: cold path — JXA, then populate cache.
    script = AccountsQueryBuilder().list_accounts()
    accounts = await execute_with_core_async(script)
    # Populate the cache with the FULL list (the map is internal and
    # needed to resolve exclusions name->UUID) but never surface a
    # hidden account to the caller.
    _get_account_map().load_from_jxa(accounts)
    return [a for a in accounts if a.get("name") not in excluded]


@mcp.tool
async def list_mailboxes(account: str | None = None) -> list[Mailbox]:
    """
    List all mailboxes for an email account.

    Args:
        account: Account name, or "all" to list across EVERY visible
                 account in one call — which is what you want for a
                 survey or a triage pass; without it you would need one
                 call per account and would have to know their names
                 first. Uses APPLE_MAIL_DEFAULT_ACCOUNT env var or the
                 first account if not specified.

    Returns:
        List of mailbox dictionaries with 'name' and 'unreadCount' fields.

    Example:
        >>> list_mailboxes("Work")
        [{"name": "INBOX", "unreadCount": 5}, ...]
    """
    if _hidden_account(account):
        # Hidden account: do not list its mailboxes, do not fall to JXA.
        return []
    # Resolve None/excluded-default to a visible account so JXA never
    # implicitly lists a hidden account's mailboxes.
    resolved = await _resolve_visible_account(account)
    if resolved is None and _excluded_account_names():
        return []
    script = AccountsQueryBuilder().list_mailboxes(resolved)
    return await execute_with_core_async(script)


@mcp.tool
async def get_emails(
    account: str | None = None,
    mailbox: str | None = None,
    filter: Literal[
        "all", "unread", "flagged", "today", "last_7_days", "this_week"
    ] = "all",
    limit: int = 50,
    before: str | None = None,
    before_id: int | None = None,
    after: str | None = None,
    offset: int = 0,
) -> list[EmailSummary]:
    """
    Get emails from a specific mailbox with optional filtering.

    Note: This tool lists emails from a single mailbox. To search
    across all mailboxes, use the search() tool instead.

    Args:
        account: Account name. Uses APPLE_MAIL_DEFAULT_ACCOUNT env var or
                 first account if not specified.
        mailbox: Mailbox name. Uses APPLE_MAIL_DEFAULT_MAILBOX env var or
                 "Inbox" if not specified.
        filter: Filter type:
            - "all": All emails (default)
            - "unread": Only unread emails
            - "flagged": Only flagged emails
            - "today": Emails received today
            - "last_7_days": Emails received in the last 7 days
            - "this_week": Alias for last_7_days
        limit: Maximum number of emails to return (default: 50)
        before: ISO date/datetime — only messages received strictly
            before it. This is how you walk a mailbox backwards: pass
            the oldest `date_received` you have seen to get the next
            page. Stable while new mail arrives, unlike `offset`.
        after: ISO date/datetime — only messages received after it.
        offset: Skip this many of the newest matches. Simpler than
            `before`, but a message arriving mid-walk shifts every
            later page by one.

    Returns:
        List of email dictionaries sorted by date (newest first).

    Examples:
        >>> get_emails()  # All emails from default mailbox
        >>> get_emails(filter="unread", limit=10)  # Unread emails
        >>> get_emails("Work", "INBOX", filter="today")  # Today's work emails
    """
    limit, offset = _validate_pagination(limit, offset)
    before_ts = _parse_date_bound(before, "before")
    # Against the PARSED bound, not the raw argument: "" and "   " are
    # None once parsed, so testing `before` let an empty timestamp
    # through and then returned the NEWEST page — which a caller paging
    # backwards reads as "start again", an endless loop over the same
    # rows. The two halves of the cursor belong together.
    if before_id is not None and before_ts is None:
        raise ValueError(
            "`before_id` is the second half of a cursor and needs "
            "`before` as well: pass the `date_received` and the `id` of "
            "the oldest row you have seen."
        )
    after_ts = _parse_date_bound(after, "after")
    all_accounts = isinstance(account, str) and account.strip().lower() in (
        "all",
        "*",
    )
    if all_accounts:
        # The Envelope Index query already means "every account" when
        # given no UUID; only this tool's defaulting stood in the way.
        # Excluded accounts stay excluded — the filter runs on UUIDs
        # further down, not on this name.
        account = None
    if not all_accounts and _hidden_account(account):
        # Hidden account explicitly requested: return nothing and do
        # NOT fall through to JXA (which would surface its mail).
        return []
    # Resolve None/excluded-default to a visible account so neither the
    # fast path nor the JXA fallback implicitly targets a hidden one.
    target_account = (
        None if all_accounts else (await _resolve_visible_account(account))
    )
    if (
        not all_accounts
        and target_account is None
        and _excluded_account_names()
    ):
        # No visible account at all (every account is hidden): the JXA
        # fallback would target Mail.accounts()[0] — a hidden one.
        return []
    # With "all" and no explicit mailbox, do not apply the INBOX
    # default: it would narrow six accounts to whichever of them
    # happens to have a mailbox by that name — and on a localized Mail
    # none of them does.
    target_mailbox = (
        None
        if (all_accounts and mailbox is None)
        else _resolve_mailbox(mailbox)
    )

    # Strategy 0: direct read against Apple's Envelope Index SQLite.
    # 100-1000x faster than JXA at scale because we skip the
    # osascript spawn + Apple Event IPC. `messages.read`,
    # `messages.flagged`, and `messages.date_received` are direct
    # columns on the Envelope Index, so every filter is served
    # without falling back to JXA for "live" state. Falls through
    # to Strategy 1 only on path / schema errors.
    try:
        from .index.disk import find_mail_directory
        from .index.envelope_direct import (
            MailboxNotFoundError,
            envelope_index_path,
            fetch_recent_messages,
        )

        mail_dir = find_mail_directory()
        env_path = envelope_index_path(mail_dir)
        if env_path.exists():
            # Resolve display-name -> UUID via the cache. Hydrates
            # the cache via JXA on first hit; subsequent calls are
            # in-process.
            await _get_account_map().ensure_loaded()
            excluded_names = _excluded_account_names()
            excluded_uuids = _get_account_map().names_to_uuids(excluded_names)
            account_uuid: str | None = None
            include_uuids: set[str] | None = None
            if target_account:
                account_uuid = _get_account_map().name_to_uuid(target_account)
            elif all_accounts:
                # Every account Mail actually HAS — not every account
                # the Envelope Index still holds rows for. Apple keeps
                # those after an account is removed, and an unscoped
                # query handed them back under their bare UUID as if
                # they were a visible account.
                account_uuid = None
                include_uuids = {
                    acct["id"]
                    for acct in (_get_account_map().get_cached_accounts() or [])
                    if acct["name"] not in excluded_names
                }
            else:
                # No account requested: scope to the first account,
                # matching the documented behavior and the JXA path
                # (which would otherwise see a different result set).
                # Skip hidden accounts so the default never lands on one.
                cached = _get_account_map().get_cached_accounts() or []
                for acct in cached:
                    if acct["name"] not in excluded_names:
                        account_uuid = acct["id"]
                        break

            # An empty cache is not "every account". `ensure_loaded()`
            # talks to Mail, and when that is slow or fails — a cold
            # cache raced by a concurrent call, Apple Events refused —
            # `account_uuid` stayed None and the query below ran
            # UNSCOPED. A single-account listing then silently answered
            # for all of them: observed as a bare get_emails() returning
            # mail from an account the caller never asked about, and
            # only when it was issued alongside other calls.
            default_unresolved = (
                not target_account and not all_accounts and account_uuid is None
            )

            if (target_account and account_uuid is None) or default_unresolved:
                # Fall through to JXA, which resolves the default
                # account properly and reports an unknown one, rather
                # than silently widening the query to every account.
                # Unknown account name. Fall through to JXA, which
                logger.debug(
                    "Account %r unresolved in AccountMap; falling back "
                    "to JXA rather than querying every account",
                    target_account,
                )
            else:
                rows = await asyncio.to_thread(
                    fetch_recent_messages,
                    env_path,
                    account_uuid=account_uuid,
                    mailbox_name=target_mailbox,
                    filter_kind=filter,
                    limit=limit,
                    before=before_ts,
                    before_id=before_id,
                    after=after_ts,
                    offset=offset,
                    exclude_account_uuids=excluded_uuids,
                    include_account_uuids=include_uuids,
                )
                visible = [
                    r
                    for r in rows
                    # Belt-and-suspenders: drop any hidden-account rows
                    # that slip through an unscoped (cold-cache) query.
                    if r.account_uuid not in excluded_uuids
                ]
                # The Envelope Index has no RFC822 header, so take it
                # from our own index in one batched statement. Missing
                # rows simply yield None — never a wrong header.
                headers: dict[tuple[str, str, int], str] = {}
                manager = _get_index_manager()
                if visible and manager.has_index():
                    try:
                        headers = await asyncio.to_thread(
                            manager.get_rfc822_ids,
                            [
                                (r.account_uuid, r.mailbox_name, r.message_id)
                                for r in visible
                            ],
                        )
                    except Exception as exc:
                        logger.debug("stable-id lookup failed: %s", exc)
                summaries = [
                    EmailSummary(
                        id=r.message_id,
                        # Only under "all": a single-account listing
                        # already knows its account, and adding the
                        # field there would change every existing
                        # response for no gain.
                        **(
                            {
                                "account": _get_account_map().uuid_to_name(
                                    r.account_uuid
                                )
                                or r.account_uuid
                            }
                            if all_accounts
                            else {}
                        ),
                        message_id=headers.get(
                            (r.account_uuid, r.mailbox_name, r.message_id)
                        ),
                        subject=r.subject,
                        sender=r.sender,
                        date_received=to_local_iso(r.date_received),
                        read=r.read,
                        flagged=r.flagged,
                    )
                    for r in visible
                ]
                # Flag colours for the whole page in one call per
                # mailbox. Without this the caller has to fetch each
                # message on its own just to learn its colour, which is
                # one process spawn per message.
                by_box: dict[tuple[str, str], list[dict]] = {}
                for summary, r in zip(summaries, visible, strict=True):
                    if summary.get("flagged"):
                        key = (
                            _get_account_map().uuid_to_name(r.account_uuid),
                            r.mailbox_name,
                        )
                        by_box.setdefault(key, []).append(summary)
                for (acct_name, box), group in by_box.items():
                    await _overlay_flag_colors_bulk(group, acct_name, box)
                return summaries
    except (
        FileNotFoundError,
        sqlite3.OperationalError,
        MailboxNotFoundError,
    ) as exc:
        logger.debug(
            "Envelope Index fast path unavailable (%s); falling back to JXA",
            exc,
        )

    if before_ts is not None or after_ts is not None or offset:
        # The JXA fallback has no date window and no offset. Silently
        # dropping them would answer a different question than the one
        # asked — the caller would page through the same newest N
        # forever and conclude the backlog is empty.
        raise ValueError(
            "`before`, `after` and `offset` need Apple's Envelope Index, "
            "which is not readable right now. Call get_index_status() "
            "for the reason."
        )

    if all_accounts:
        # Only the Envelope Index can answer across accounts. JXA walks
        # one account at a time, so falling through would quietly
        # answer a different question than the one that was asked.
        raise ValueError(
            "Listing across all accounts needs Apple's Envelope Index, "
            "which is not readable right now (Full Disk Access, or an "
            "unsupported Mail.app layout). Call get_index_status() for "
            "the reason, or pass a single `account`."
        )

    # Strategy 1: JXA batchFetch fallback. Preserves correctness
    # when the Envelope Index path / schema is unavailable on a
    # given Mail.app build.
    query = (
        QueryBuilder()
        .from_mailbox(target_account, target_mailbox)
        .select("standard")
    )

    if filter == "unread":
        query = query.where("data.readStatus[i] === false")
    elif filter == "flagged":
        query = query.where("data.flaggedStatus[i] === true")
    elif filter == "today":
        query = query.where("data.dateReceived[i] >= MailCore.today()")
    elif filter in ("last_7_days", "this_week"):
        query = query.where("data.dateReceived[i] >= MailCore.daysAgo(7)")

    query = query.order_by("date_received", descending=True).limit(limit)

    try:
        rows = await execute_query_async(query)
        for row in rows:
            if "date_received" in row:
                row["date_received"] = to_local_iso(row["date_received"])
        return rows
    except Exception as exc:
        # An unknown mailbox makes JXA fail with a raw "...Error:
        # Error: Can't get object. (-1728)". Surface a clean,
        # model-friendly message; re-raise other failures intact.
        raw = str(exc)
        msg = raw.lower()
        if "no mailbox matching" in msg:
            # The resolver already listed what the account really has —
            # on a system whose language we do not cover, that list IS
            # the answer. Pass it through instead of flattening it into
            # a bare "not found".
            available = raw.split("Available:", 1)[-1].strip()
            # `None` is not an account name. When no account was given
            # the message has to name the default, or it reads like a
            # corrupted request.
            where = (
                repr(target_account)
                if target_account
                else "the default account"
            )
            raise ValueError(
                f"Mailbox {target_mailbox!r} does not exist in "
                f"{where}. This account's mailboxes are: "
                f"{available}. Call list_mailboxes() and pass one of "
                f"these as `mailbox`; Mail names them in the system "
                f"language, so the English default may not apply here."
            ) from None
        if "-1728" in msg or "can't get object" in msg:
            raise ValueError(
                f"Mailbox {target_mailbox!r} not found"
                f" in account {target_account!r}."
            ) from None
        raise


def _build_attachment_js() -> str:
    """Return JXA snippet to extract attachment metadata from `msg`."""
    return """
let attachments = [];
try {
    const atts = msg.mailAttachments();
    if (atts && atts.length > 0) {
        for (let a of atts) {
            try {
                attachments.push({
                    filename: a.name(),
                    mime_type: a.mimeType() || 'application/octet-stream',
                    size: a.fileSize() || 0
                });
            } catch(ae) {}
        }
    }
} catch(e) {}
"""


def _build_get_email_script(message_id: int, mailbox_setup: str) -> str:
    """Build JXA script to fetch a single email by ID.

    Extracted to avoid duplication between the primary and
    fallback fetch strategies.
    """
    att_js = _build_attachment_js()
    return f"""
const targetId = {message_id};
let msg = null;
{mailbox_setup}

const ids = mailbox.messages.id();
const idx = ids.indexOf(targetId);
if (idx !== -1) {{
    msg = mailbox.messages[idx];
}}

if (!msg) {{
    throw new Error('Message not found with ID: ' + targetId);
}}

{att_js}

JSON.stringify({{
    id: msg.id(),
    subject: msg.subject(),
    sender: msg.sender(),
    content: msg.content(),
    date_received: MailCore.formatDate(msg.dateReceived()),
    date_sent: MailCore.formatDate(msg.dateSent()),
    read: msg.readStatus(),
    flagged: msg.flaggedStatus(),
    flag_color: (function () {{
        try {{ return MailCore.flagColorName(msg.flagIndex()); }}
        catch (e) {{ return null; }}
    }})(),
    reply_to: msg.replyTo(),
    message_id: msg.messageId(),
    mailbox: (function () {{
        try {{ return String(mailbox.name()); }} catch (e) {{ return null; }}
    }})(),
    account: (function () {{
        try {{ return String(mailbox.account().name()); }}
        catch (e) {{ return null; }}
    }})(),
    attachments: attachments
}});
"""


MAX_READ_BATCH = 50


@mcp.tool
async def get_email(
    message_id: int | str | list[int | str],
    account: str | None = None,
    mailbox: str | None = None,
) -> EmailFull | list[dict]:
    """
    Get a single email with full content.

    Looks up the email across all accounts using a cascade strategy.
    Just pass the message_id — account/mailbox are optional hints
    that speed up lookup but are not required.

    Args:
        message_id: One reference, or a LIST of them (max 50). A batch
            costs one round-trip instead of one per message, and each
            read is a few milliseconds from disk — so fetching the page
            you just listed is nearly free. A list returns a list of
            {"ref": ..., "email": {...}} or {"ref": ..., "error": "..."}
            entries, in the order given: one unreadable message never
            takes the batch down.
        account: Optional hint (speeds up lookup, not required)
        mailbox: Optional hint (speeds up lookup, not required)

    Returns:
        Email dictionary with full content including:
        - id: the numeric id — a per-mailbox ROWID, valid only while
          the message stays where it is
        - message_id: the RFC822 Message-ID header. Pass THIS to
          set_flag / set_read_status; it keeps working after the mail
          is filed elsewhere from another device.
        - subject, sender, date_received, date_sent
        - content: Full plain text body
        - read, flagged status
        - reply_to
        - attachments: List of {filename, mime_type, size}

    Note:
        The attachments list comes from JXA's mailAttachments(),
        which only reports file attachments visible in Mail.app's
        UI. Inline images, S/MIME signatures, and attachments in
        sent/bounce-back emails may not appear. Use get_attachment
        with a known filename for reliable extraction from disk.

    Example:
        >>> get_email("<a1b2@example.com>")  # stable, preferred
        >>> get_email(12345)  # numeric id also works
        {"id": 12345, "subject": "Meeting notes",
         "content": "Hi team,\\n\\nHere are the notes...", ...}
    """
    refs = _normalize_message_ids(message_id)
    if not isinstance(message_id, (list, tuple)) and len(refs) == 1:
        # A single reference keeps the single-object shape it always
        # had — callers and their parsers depend on it.
        one = refs[0]
        if isinstance(one, str):
            return await _get_email_by_header(one, account, mailbox)
        return await _get_email_by_id(one, account, mailbox)

    if len(refs) > MAX_READ_BATCH:
        raise ValueError(
            f"Too many references ({len(refs)}); max {MAX_READ_BATCH} per "
            f"call. Each one is a disk read of a few milliseconds, so the "
            f"limit is about how much text fits in your context, not "
            f"speed — split the list."
        )

    async def fetch(ref: MessageRef) -> dict:
        try:
            if isinstance(ref, str):
                got = await _get_email_by_header(ref, account, mailbox)
            else:
                got = await _get_email_by_id(ref, account, mailbox)
            return {"ref": ref, "email": got}
        except Exception as exc:
            # One unreadable message must not take the batch down with
            # it — the same bucket contract the write tools follow.
            return {"ref": ref, "error": str(exc)}

    return list(await asyncio.gather(*(fetch(r) for r in refs)))


class _LiveLookupIncomplete(RuntimeError):
    """The live search did not cover everything it would have needed to.

    Distinct from "the message is not there": a capped scan, a mailbox
    Mail refused to read, a timeout or a denied Automation permission
    all leave the question OPEN. Reporting them as a missing message
    turns an incomplete search into a verdict — the defect that cost a
    day of debugging in its write-path form.
    """


async def _locate_header_via_jxa(
    header: str, account: str | None = None
) -> tuple[str, str, int] | None:
    """Find a message by its RFC822 header without using the index.

    Returns ``(account, mailbox, id)``, or None when every mailbox was
    searched and the header was genuinely absent.

    Raises:
        _LiveLookupIncomplete: the search could not be completed, so
            nothing may be concluded about the message.
    """
    if account:
        # `account` narrows the SEARCH, not the result. Filtering
        # afterwards was a silent miss: the scan stops at its first hit,
        # so a copy of the same message in another account ended the
        # search, was then dropped by the filter, and the requested
        # account's copy — never looked at — was reported missing.
        accounts = [account]
    else:
        accounts = await _visible_account_names()
        if not accounts:
            one = await _resolve_visible_account(None)
            accounts = [one] if one else []
    if not accounts:
        raise _LiveLookupIncomplete("no visible account could be resolved")
    script = f"""
const targets = {json.dumps(accounts)};
const needle = MailCore.normHeaderValue({json.dumps(header)});
let hit = null;
let capped = 0;      // mailboxes past the scan limit
let unreadable = 0;  // mailboxes Mail refused
for (const name of targets) {{
    if (hit) break;
    let account;
    try {{ account = MailCore.getAccount(name); }}
    catch (e) {{ unreadable++; continue; }}
    let boxes;
    try {{ boxes = account.mailboxes(); }}
    catch (e) {{ unreadable++; continue; }}
    const limit = Math.min(boxes.length, {STRATEGY3_MAX_MAILBOXES});
    capped += Math.max(0, boxes.length - limit);
    for (let m = 0; m < limit && !hit; m++) {{
        let ids;
        try {{ ids = boxes[m].messages.messageId(); }}
        catch (e) {{ unreadable++; continue; }}
        for (let i = 0; i < ids.length; i++) {{
            if (MailCore.normHeaderValue(ids[i]) === needle) {{
                hit = {{
                    account: name,
                    mailbox: String(boxes[m].name()),
                    id: boxes[m].messages[i].id(),
                }};
                break;
            }}
        }}
    }}
}}
JSON.stringify({{hit: hit, capped: capped, unreadable: unreadable}});
"""
    try:
        res = await execute_with_core_async(script, timeout=RECOVERY_TIMEOUT)
    except Exception as exc:
        # Timeout, refused Apple Events, Mail not running: the search
        # never happened. Saying "not found" here would be a lie.
        raise _LiveLookupIncomplete(str(exc)) from exc
    if not isinstance(res, dict):
        raise _LiveLookupIncomplete("the live search returned no answer")
    hit = res.get("hit")
    if hit:
        return hit["account"], hit["mailbox"], int(hit["id"])
    skipped = int(res.get("capped") or 0) + int(res.get("unreadable") or 0)
    if skipped:
        raise _LiveLookupIncomplete(
            f"{skipped} mailbox(es) were not searched (scan limit or "
            f"unreadable), so the message may still exist in one of them"
        )
    return None


async def _get_email_by_header(
    header: str,
    account: str | None,
    mailbox: str | None,
) -> EmailFull:
    """Fetch by RFC822 Message-ID, verifying what comes back.

    The index maps the header to a ``(account, mailbox, ROWID)``, but
    that row may be stale — the message may since have been filed
    elsewhere, and the ROWID now belong to a *different* message. So
    every candidate is fetched and then checked against the header that
    was asked for; a mismatch moves on to the next candidate rather
    than returning the wrong email.
    """
    header = header.strip()
    manager = _get_index_manager()
    if not manager.has_index():
        raise ValueError(
            f"Cannot look up {header!r}: no search index. Call "
            f"get_index_status() — it explains how to build one — or "
            f"pass the numeric id instead."
        )

    candidates = await asyncio.to_thread(manager.find_by_rfc822, header)
    if not candidates:
        # Not indexed yet — the normal state for anything that arrived
        # after the last sync. Ask Apple Mail directly rather than
        # declaring a message missing that is sitting in a mailbox.
        try:
            live = await _locate_header_via_jxa(header)
        except _LiveLookupIncomplete as exc:
            raise ValueError(
                f"Email {header!r} is not in the index, and the live "
                f"search could not be completed: {exc}. This says "
                f"nothing about whether the message exists — retry, "
                f"pass `account` to narrow the search, or call "
                f"refresh_index() so the index can place it directly."
            ) from None
        if live is None:
            raise ValueError(
                f"Email {header!r} was not found in the index and not in "
                f"any visible account; every mailbox was searched, so it "
                f"was most likely deleted."
            )
        acct_name, mbox, rowid = live
        result = await _get_email_by_id(rowid, acct_name, mbox)
        if _header_key(result.get("message_id")) == _header_key(header):
            return result
        raise ValueError(f"Email {header!r} could not be retrieved.")

    acct_map = _get_account_map()
    excluded = _excluded_account_names()
    if excluded:
        await acct_map.ensure_loaded()

    last_error: Exception | None = None
    for acct_uuid, mbox, rowid in candidates:
        acct_name = acct_map.uuid_to_name(acct_uuid)
        if acct_name in excluded:
            continue
        if account and acct_name != account:
            continue
        try:
            result = await _get_email_by_id(rowid, acct_name, mbox)
        except Exception as exc:  # try the next known copy
            last_error = exc
            continue
        if _header_key(result.get("message_id")) == _header_key(header):
            return result
        # Stale row: that ROWID is somebody else's message now.
        logger.debug(
            "Stale index row for %s in %s/%s — ROWID %s now holds %r",
            header,
            acct_name,
            mbox,
            rowid,
            result.get("message_id"),
        )

    if last_error is not None:
        logger.debug("header lookup last error: %s", last_error)
    # Every indexed location was stale. That says the INDEX is out of
    # date, not that the message is gone — ask Mail before concluding
    # anything: "the index orders the search, it never limits it".
    try:
        live = await _locate_header_via_jxa(header, account)
    except _LiveLookupIncomplete as exc:
        raise ValueError(
            f"Email {header!r} could not be reached: every location on "
            f"record is stale, and the live search could not be "
            f"completed either ({exc}). This says nothing about whether "
            f"the message exists."
        ) from None
    if live is not None:
        acct_name, mbox, rowid = live
        if acct_name not in _excluded_account_names():
            try:
                result = await _get_email_by_id(rowid, acct_name, mbox)
            except Exception as exc:
                logger.debug("live candidate unreadable: %s", exc)
            else:
                if _header_key(result.get("message_id")) == _header_key(header):
                    return result

    raise ValueError(
        f"Email {header!r} could not be retrieved: every location on "
        f"record is stale, and Mail does not have it either. Call "
        f"refresh_index() and try again."
    )


async def _get_email_by_id(
    message_id: int,
    account: str | None = None,
    mailbox: str | None = None,
) -> EmailFull:
    """The numeric-id cascade (Strategies 0-3). See ``get_email``."""
    if _hidden_account(account):
        # Hidden account: surface as a plain "not found" so a hidden
        # account is indistinguishable from a missing message, and do
        # not fall through to any strategy that would fetch it live.
        raise ValueError(f"Email {message_id} not found.")
    # Resolve None/excluded-default to a visible account so the JXA
    # strategies never implicitly scan a hidden default account.
    resolved_account = await _resolve_visible_account(account)
    if resolved_account is None and _excluded_account_names():
        # No visible account at all (every account is hidden): the JXA
        # strategies would scan Mail.accounts()[0] — a hidden one.
        raise ValueError(f"Email {message_id} not found.")
    resolved_mailbox = _resolve_mailbox(mailbox)

    def _with_location(
        result: dict, account: str | None, mailbox: str | None
    ) -> dict:
        """Record where the message was actually found."""
        if account and not result.get("account"):
            result["account"] = account
        if mailbox and not result.get("mailbox"):
            result["mailbox"] = mailbox
        return result

    def _enrich_attachments(result: dict) -> dict:
        """Finalize a get_email result: richer attachments + local time.

        Every return path of this tool goes through here, so it is the
        one place where stored UTC becomes the reader's local time.
        """
        for field in ("date_received", "date_sent"):
            if field in result:
                result[field] = to_local_iso(result[field])
        # One spelling of the header, whichever strategy answered. The
        # `.emlx` keeps the angle brackets, Apple's `messageId` drops
        # them — so the same message came back as "<a@b>" from disk and
        # "a@b" from JXA, and a caller comparing strings saw two
        # different messages. Brackets are the RFC form and what
        # search() and get_emails() already hand out.
        header = result.get("message_id")
        if isinstance(header, str) and header.strip():
            bare = header.strip()
            if not bare.startswith("<"):
                result["message_id"] = f"<{bare}>"
        try:
            mgr = _get_index_manager()
            if mgr.has_index():
                idx_atts = mgr.get_email_attachments(message_id)
                if idx_atts and len(idx_atts) > len(
                    result.get("attachments", [])
                ):
                    result["attachments"] = idx_atts
        except Exception:
            pass
        return result

    # Strategy 0: Read directly from .emlx file on disk (fastest, no JXA)
    # Stale-entry detection: if find_email_path returns a path but the file
    # is gone (deleted/moved between syncs), capture the (account, mailbox)
    # for cleanup outside the broad except block.
    stale_index_entry: tuple[str | None, str | None] | None = None
    try:
        manager = _get_index_manager()
        if manager.has_index():
            from .index.disk import parse_emlx

            acct_map = _get_account_map()
            await acct_map.ensure_loaded()

            idx_acct = None
            if account is not None:
                idx_acct = acct_map.name_to_uuid(account)

            excluded_uuids = acct_map.names_to_uuids(_excluded_account_names())

            emlx_path = manager.find_email_path(
                message_id, account=idx_acct, mailbox=mailbox
            )
            # The disk read knows the file but not its place; ask the
            # index, so the answer can say where the message lives.
            disk_loc = manager.find_email_location(
                message_id, account=idx_acct, mailbox=mailbox
            )
            loc_account = (
                acct_map.uuid_to_name(disk_loc[0]) if disk_loc else None
            )
            loc_mailbox = disk_loc[1] if disk_loc else None
            if emlx_path:
                # A stale index row (account excluded after indexing,
                # before re-sync) could still resolve here.
                if _path_in_excluded_account(emlx_path, excluded_uuids):
                    raise _AccountHiddenError(f"Email {message_id} not found.")
                if emlx_path.exists():
                    parsed = await asyncio.to_thread(parse_emlx, emlx_path)
                    if parsed:
                        result = {
                            "id": parsed.id,
                            "subject": parsed.subject,
                            "sender": parsed.sender,
                            "content": parsed.content,
                            "date_received": parsed.date_received,
                            "date_sent": parsed.date_sent,
                            "read": parsed.read
                            if parsed.read is not None
                            else False,
                            "flagged": parsed.flagged
                            if parsed.flagged is not None
                            else False,
                            "reply_to": parsed.reply_to,
                            "message_id": parsed.message_id_header,
                            "attachments": [
                                {
                                    "filename": a.filename,
                                    "mime_type": a.mime_type,
                                    "size": a.file_size,
                                }
                                for a in (parsed.attachments or [])
                            ],
                        }
                        await _overlay_live_flags(result, message_id)
                        await _overlay_flag_color(
                            result, message_id, loc_account, loc_mailbox
                        )
                        return _enrich_attachments(
                            _with_location(result, loc_account, loc_mailbox)
                        )
                else:
                    stale_index_entry = (idx_acct, mailbox)
    except _AccountHiddenError:
        raise
    except Exception:
        logger.debug(
            "Strategy 0 (disk) failed for %s, falling through",
            message_id,
            exc_info=True,
        )

    # Stale-entry handling: clean up the dead row, then KEEP GOING.
    # A missing .emlx means the recorded path is wrong — nothing more.
    # The message may well still be in Mail: it was re-filed, Mail
    # rebuilt its store, or the row simply predates a move. The old
    # code raised "deleted or moved" here on the assumption that the
    # live strategies would fail anyway. That assumption was never
    # checked, and stating it as fact is the same defect this whole
    # review pass was about.
    if stale_index_entry is not None:
        stale_acct, stale_mb = stale_index_entry
        try:
            manager = _get_index_manager()
            deleted = manager.delete_email(
                message_id, account=stale_acct, mailbox=stale_mb
            )
            logger.info(
                "Cleaned %d stale index entry/entries for message %s",
                deleted,
                message_id,
            )
        except Exception:
            logger.warning(
                "Failed to clean stale index entry for %s",
                message_id,
                exc_info=True,
            )
        # Fall through to the live strategies below.

    # Strategy 1: Try specified mailbox
    mailbox_setup = build_mailbox_setup_js(resolved_account, resolved_mailbox)
    script = _build_get_email_script(message_id, mailbox_setup)

    try:
        result = await execute_with_core_async(script)
        return _enrich_attachments(
            _with_location(result, resolved_account, resolved_mailbox)
        )
    except Exception:
        pass  # Fall through to strategy 2

    # Strategy 2: Index lookup — find the email's real location
    # Only scope by account/mailbox when the caller explicitly provided them
    # (not when they were filled in from defaults — strategy 1 already tried
    # the default location and failed).
    try:
        manager = _get_index_manager()
        if manager.has_index():
            acct_map = _get_account_map()
            await acct_map.ensure_loaded()

            idx_acct = None
            if account is not None:
                idx_acct = acct_map.name_to_uuid(account)
            idx_mb = mailbox
            excluded_uuids = acct_map.names_to_uuids(_excluded_account_names())

            location = manager.find_email_location(
                message_id, account=idx_acct, mailbox=idx_mb
            )
            if location:
                idx_account, idx_mailbox = location
                friendly_account = acct_map.uuid_to_name(idx_account)

                # Compare by UUID, not display name: uuid_to_name falls
                # back to the raw UUID when the map can't resolve it,
                # which would slip past a name-based check.
                if idx_account in excluded_uuids:
                    # Message lives in a hidden account: refuse rather
                    # than fetch it live via JXA.
                    raise _AccountHiddenError(f"Email {message_id} not found.")

                setup = build_mailbox_setup_js(friendly_account, idx_mailbox)
                script = _build_get_email_script(message_id, setup)
                try:
                    result = await execute_with_core_async(script)
                    return _enrich_attachments(
                        _with_location(result, friendly_account, idx_mailbox)
                    )
                except _AccountHiddenError:
                    raise
                except Exception:
                    pass  # Fall through to strategy 3
    except _AccountHiddenError:
        raise
    except Exception:
        pass  # Index unavailable, fall through

    # Strategy 3: Iterate all mailboxes with per-mailbox error handling
    # Guarded with a timeout and mailbox limit to prevent runaway scans
    from .builders import GetEmailBuilder

    script = GetEmailBuilder(
        message_id=message_id,
        account=resolved_account,
        max_mailboxes=STRATEGY3_MAX_MAILBOXES,
        attachment_js=_build_attachment_js(),
    ).build()
    try:
        result = await execute_with_core_async(
            script, timeout=STRATEGY3_TIMEOUT
        )
        # The scan is the ONE strategy that answers when the index does
        # not know the message — so it is the one case where the caller
        # cannot derive the location, and it was the only return path
        # that did not record it.
        return _enrich_attachments(
            _with_location(
                result,
                result.get("account") or resolved_account,
                result.get("mailbox"),
            )
        )
    except TimeoutError:
        if account and mailbox:
            hint = (
                "The email may have been deleted or moved, "
                "or the mailbox is too large for JXA to scan. "
                "Try 'apple-mail-mcp rebuild' to refresh the index."
            )
        elif account or mailbox:
            hint = (
                "Try providing both account and mailbox for "
                "faster lookup, or rebuild the index."
            )
        else:
            hint = "Provide account/mailbox for faster lookup."
        raise TimeoutError(
            f"Could not find message {message_id} within "
            f"{STRATEGY3_TIMEOUT}s (searched up to "
            f"{STRATEGY3_MAX_MAILBOXES} mailboxes). {hint}"
        ) from None
    except Exception as exc:
        # A genuinely-missing message makes JXA fail with a raw,
        # doubled-up "...Error: Error: Message not found... (-1728)".
        # Surface a clean, model-friendly not-found for that case;
        # re-raise anything else (Mail.app down, permissions) intact.
        raw = str(exc)
        msg = raw.lower()
        if "incomplete:" in msg:
            # The scan stopped early — a mailbox cap or one Mail would
            # not read. Absence was never established, so do not claim
            # it: the caller can act on this, "not found" it cannot.
            detail = raw.split("INCOMPLETE:", 1)[-1].rstrip(")").strip()
            # The builder may already say "not searched"; appending it
            # again produced "not searched not searched".
            if not detail.endswith("not searched"):
                detail = f"{detail} not searched"
            raise ValueError(
                f"Message {message_id} was not found, but the search was "
                f"incomplete ({detail}). This does not mean "
                f"the message is gone — pass `account` and `mailbox` to "
                f"look in the right place, or use its Message-ID."
            ) from None
        if "not found" in msg or "-1728" in msg or "can't get object" in msg:
            extra = (
                " Its index entry was stale and has been removed; call "
                "refresh_index() to re-record it if it still exists."
                if stale_index_entry is not None
                else ""
            )
            # Strategy 3 walks ONE account. With several configured,
            # that is not a search of everywhere — saying "not found"
            # would claim an absence for accounts nobody looked in.
            if account is None:
                others = [
                    a
                    for a in await _visible_account_names()
                    if a and a != resolved_account
                ]
                if others:
                    raise ValueError(
                        f"Message {message_id} was not found in account "
                        f"{resolved_account!r}, and the other "
                        f"{len(others)} account(s) were not searched: a "
                        f"numeric id is only unique within a mailbox, so "
                        f"it cannot be looked for across accounts. Pass "
                        f"`account`, or use the message's Message-ID, "
                        f"which is searched everywhere.{extra}"
                    ) from None
            raise ValueError(
                f"Message {message_id} not found.{extra}"
            ) from None
        raise


class LinkResult(TypedDict):
    """A hyperlink extracted from an email."""

    url: str
    text: str


class AttachmentContent(TypedDict, total=False):
    """Content returned by get_attachment."""

    filename: str
    mime_type: str
    size: int
    file_path: str
    links: list[LinkResult]


async def _resolve_emlx_path(
    message_id: int | str,
    account: str | None = None,
    mailbox: str | None = None,
) -> _Path:
    """Resolve a message reference to an .emlx file path via the index.

    Accepts a numeric ROWID or an RFC822 Message-ID header. For a header
    the resolved file is *verified* to actually carry it before being
    handed back — an index row can be stale, and its ROWID may by then
    belong to a different message.

    Raises:
        ValueError: If the index is missing or email not found.
    """
    if isinstance(message_id, str):
        return await _resolve_emlx_path_by_header(message_id, account, mailbox)
    if _hidden_account(account):
        # Hidden account: links/attachment extractors must not reach it.
        raise ValueError(f"Email {message_id} not found.")

    manager = _get_index_manager()
    if not manager.has_index():
        raise ValueError("No search index. Run 'apple-mail-mcp index'.")

    # Loads the JXA-backed map only when needed (an account to resolve
    # or exclusions to enforce) — with neither, this path must stay
    # index-only so it keeps working when Mail.app is unavailable.
    excluded_uuids = await _excluded_account_uuids()
    idx_acct = None
    if account:
        acct_map = _get_account_map()
        await acct_map.ensure_loaded()
        idx_acct = acct_map.name_to_uuid(account) or account

    emlx_path = manager.find_email_path(
        message_id, account=idx_acct, mailbox=mailbox
    )
    if not emlx_path:
        raise ValueError(f"Email {message_id} not found in index.")
    # A stale index row for a now-excluded account resolves to a path
    # under its UUID dir; refuse to read it.
    if _path_in_excluded_account(emlx_path, excluded_uuids):
        raise ValueError(f"Email {message_id} not found.")
    return emlx_path


async def _resolve_emlx_path_by_header(
    header: str,
    account: str | None,
    mailbox: str | None,
) -> _Path:
    """Locate the .emlx file carrying a given RFC822 Message-ID.

    Every candidate the index offers is parsed and checked against the
    requested header, so a stale row yields "not found" — never another
    message's attachments or links.
    """
    from .index.disk import parse_emlx

    header = header.strip()
    manager = _get_index_manager()
    if not manager.has_index():
        raise ValueError("No search index. Run 'apple-mail-mcp index'.")

    candidates = await asyncio.to_thread(manager.find_by_rfc822, header)
    if not candidates:
        raise ValueError(f"Email {header!r} not found in index.")

    excluded_uuids = await _excluded_account_uuids()
    acct_map = _get_account_map()
    excluded_names = _excluded_account_names()
    if excluded_names or account:
        await acct_map.ensure_loaded()

    for acct_uuid, mbox, rowid in candidates:
        if acct_uuid in excluded_uuids:
            continue
        acct_name = acct_map.uuid_to_name(acct_uuid)
        if acct_name in excluded_names:
            continue
        if account and acct_name != account:
            continue
        if mailbox and mbox != mailbox:
            continue
        path = manager.find_email_path(rowid, account=acct_uuid, mailbox=mbox)
        if not path or _path_in_excluded_account(path, excluded_uuids):
            continue
        try:
            parsed = await asyncio.to_thread(parse_emlx, path)
        except Exception as exc:
            logger.debug("Cannot parse candidate %s: %s", path, exc)
            continue
        if parsed is not None and _header_key(
            parsed.message_id_header
        ) == _header_key(header):
            return path

    # Every indexed location was stale — which says the INDEX is out of
    # date, not that the message is gone. `get_email` already asks Mail
    # at this point; stopping here meant a moved message could be read
    # while its attachments and links reported it missing.
    try:
        live = await _locate_header_via_jxa(header, account)
    except _LiveLookupIncomplete as exc:
        raise ValueError(
            f"Email {header!r} could not be reached: every location on "
            f"record is stale, and the live search could not be "
            f"completed either ({exc}). This says nothing about whether "
            f"the message exists."
        ) from None
    if live is not None:
        acct_name, _mbox, rowid = live
        if acct_name not in _excluded_account_names():
            found = await _emlx_path_for_live_location(
                acct_name, rowid, header, excluded_uuids
            )
            if found is not None:
                return found

    raise ValueError(
        f"Email {header!r} could not be located on disk: every entry on "
        f"record is stale, and Mail does not have it either. Call "
        f"refresh_index() and try again."
    )


async def _emlx_path_for_live_location(
    account: str,
    rowid: int,
    header: str,
    excluded_uuids: set[str],
) -> _Path | None:
    """The `.emlx` file for a message Mail just located, or None.

    Mail hands back an account and a ROWID; the file is named after that
    ROWID, so the search is a filename match inside the one account —
    bounded, and only reached once every indexed location has already
    turned out stale. The header is verified on what is found, for the
    same reason the indexed candidates are: the file must be the message
    that was asked for, not whatever now owns that number.
    """
    from .index.disk import find_mail_directory, parse_emlx

    acct_uuid = _get_account_map().name_to_uuid(account) or account
    try:
        mail_dir = await asyncio.to_thread(find_mail_directory)
    except (FileNotFoundError, PermissionError):
        return None
    base = mail_dir / acct_uuid
    if not base.is_dir():
        return None
    for pattern in (f"{rowid}.emlx", f"{rowid}.partial.emlx"):
        for candidate in sorted(base.rglob(pattern)):
            if _path_in_excluded_account(str(candidate), excluded_uuids):
                continue
            parsed = await asyncio.to_thread(parse_emlx, candidate)
            if parsed is None:
                continue
            if _header_key(parsed.message_id_header) == _header_key(header):
                return candidate
    return None


@mcp.tool
async def get_email_links(
    message_id: int | str,
    account: str | None = None,
    mailbox: str | None = None,
) -> dict:
    """
    Extract hyperlinks from an email's HTML content.

    Filters out mailto:, javascript:, and long tracking URLs.
    Returns deduplicated links with their anchor text.

    Requires the search index.

    Args:
        message_id: The email's numeric id, or its RFC822 Message-ID
            header (preferred — it survives the mail being moved)
        account: Account name (optional, speeds up lookup)
        mailbox: Mailbox name (optional, speeds up lookup)

    Returns:
        Dict with 'links' list, each having 'url' and 'text'.

    Example:
        >>> get_email_links(12345)
        {"links": [{"url": "https://...", "text": "Click"}]}
    """
    emlx_path = await _resolve_emlx_path(message_id, account, mailbox)
    from .index.disk import get_email_links as _get_links

    link_infos = await asyncio.to_thread(_get_links, emlx_path)
    return {
        "links": [{"url": li.url, "text": li.text} for li in link_infos],
    }


@mcp.tool
async def get_email_attachment(
    message_id: int | str,
    filename: str,
    account: str | None = None,
    mailbox: str | None = None,
) -> AttachmentContent:
    """
    Extract a file attachment from an email and save to disk.

    Saves the attachment under ~/.apple-mail-mcp/attachments/.
    Parses the raw MIME structure, so it works for all attachment
    types including inline images and S/MIME signatures.

    Requires the search index.

    Args:
        message_id: The email's numeric id, or its RFC822 Message-ID
            header (preferred — it survives the mail being moved)
        filename: Attachment filename to extract
        account: Account name (optional, speeds up lookup)
        mailbox: Mailbox name (optional, speeds up lookup)

    Returns:
        Dict with filename, mime_type, size, and file_path
        pointing to the saved file.

    Example:
        >>> get_email_attachment(12345, "invoice.pdf")
        {"filename": "invoice.pdf", "file_path": "/...", ...}
    """
    # Clean up old cached attachments (best-effort)
    try:
        await asyncio.to_thread(_cleanup_old_attachments)
    except Exception:
        pass

    emlx_path = await _resolve_emlx_path(message_id, account, mailbox)
    from .index.disk import get_attachment_content

    result = await asyncio.to_thread(
        get_attachment_content, emlx_path, filename
    )
    if result is None:
        raise ValueError(
            f"Attachment '{filename}' not found in email {message_id}."
        )

    raw_bytes, mime_type = result

    # Save to unique subdirectory (0o700 for sensitive content).
    # File itself is chmod'd to 0o600 so it stays owner-only even if a
    # later refactor changes the dir permissions or copies the file out.
    ATTACHMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENT_CACHE_DIR.chmod(0o700)
    save_dir = _Path(tempfile.mkdtemp(dir=ATTACHMENT_CACHE_DIR))
    safe_name = _Path(filename).name
    file_path = save_dir / safe_name
    file_path.write_bytes(raw_bytes)
    file_path.chmod(0o600)

    return {
        "filename": safe_name,
        "mime_type": mime_type,
        "size": len(raw_bytes),
        "file_path": str(file_path),
    }


@mcp.tool
async def get_attachment(
    message_id: int | str,
    filename: str | None = None,
    account: str | None = None,
    mailbox: str | None = None,
) -> AttachmentContent:
    """
    DEPRECATED: Use get_email_attachment() or get_email_links().

    Extract resources from an email: attachments or links.
    Delegates to get_email_links (filename omitted) or
    get_email_attachment (filename provided).

    Args:
        message_id: The email's numeric id, or its RFC822 Message-ID
            header (preferred — it survives the mail being moved)
        filename: Attachment filename to extract. If omitted,
            returns links instead.
        account: Account name (optional)
        mailbox: Mailbox name (optional)
    """
    if filename is None:
        return await get_email_links(message_id, account, mailbox)
    return await get_email_attachment(message_id, filename, account, mailbox)


@mcp.tool
async def search(
    query: str,
    account: str | None = None,
    mailbox: str | None = None,
    scope: Literal["all", "subject", "sender", "body", "attachments"] = "all",
    limit: int = 20,
    offset: int = 0,
    exclude_mailboxes: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    highlight: bool = False,
) -> list[SearchResult] | dict:
    """
    Search emails across all accounts and mailboxes.

    Uses full-text search index for fast results (~2ms). All scopes
    search across every account and mailbox unless filtered.

    Query tips:
    - Use 2-3 specific keywords, not full sentences
    - Terms are AND-ed: "budget Q1" finds emails with BOTH words
    - Use quotes for exact phrases: '"quarterly report"'
    - Prefix search: "meet*" matches meeting, meetings, etc.
    - The default scope "all" searches subject + sender + body together

    Args:
        query: Search keywords (2-3 specific terms work best).
            Do NOT use long natural-language phrases.
        account: Filter to specific account name (optional).
        mailbox: Filter to specific mailbox (optional).
        scope: Where to search:
            - "all": Subject + sender + body (default, recommended)
            - "subject": Subject line only
            - "sender": Sender name/email only
            - "body": Body text only
            - "attachments": Attachment filenames
        limit: Maximum results (default: 20)
        offset: Skip first N results for pagination (default: 0)
        exclude_mailboxes: Mailboxes to exclude (default: ["Drafts"])
        before: Exclude emails on/after this date (YYYY-MM-DD).
        after: Include only emails on/after this date (YYYY-MM-DD).
        highlight: Wrap matched terms in **markers** in subject
            and content_snippet (default: False).

    Returns:
        List of matching emails with id, message_id, subject, sender,
        date_received, score, matched_in, content_snippet,
        account, and mailbox fields.

        `message_id` is the RFC822 Message-ID header — the stable
        handle. Pass it to set_flag / set_read_status rather than the
        numeric `id`, which is a per-mailbox ROWID and stops resolving
        as soon as any device files the message elsewhere.

        When nothing matches, returns a dict instead:
        {"result": [], "hint": "..."} — the hint suggests how to
        adjust the query (fewer keywords, different scope).

    Examples:
        >>> search("Kim Foulds")  # Find person across all fields
        >>> search("quarterly budget")  # Keywords, not sentences
        >>> search('"project update"')  # Exact phrase
        >>> search("invoice.pdf", scope="attachments")
        >>> search("budget", after="2026-01-01", before="2026-04-01")
    """
    limit, offset = _validate_pagination(limit, offset)
    before = _validate_date(before, "before")
    after = _validate_date(after, "after")
    if exclude_mailboxes is None:
        exclude_mailboxes = ["Drafts"]

    if _hidden_account(account):
        # Hidden account explicitly requested: no results, no fallback.
        return []

    _EMPTY_HINT = (
        "No results. Try fewer keywords (2-3 specific terms), "
        "check spelling, or use scope='all' to search everywhere. "
        "If searches keep coming up empty, call get_index_status() — "
        "the index may be missing, still building, or blocked by "
        "macOS permissions, and it will say how to fix that."
    )

    def _maybe_hint(results: list) -> list | dict:
        if not results:
            return {"result": [], "hint": _EMPTY_HINT}
        return results

    # Attachment filename search (SQL LIKE query, no JXA needed)
    if scope == "attachments":
        manager = _get_index_manager()
        if not manager.has_index():
            return []

        acct_map = _get_account_map()
        await acct_map.ensure_loaded()
        search_acct = (
            acct_map.name_to_uuid(account) or account if account else None
        )
        excluded_uuids = acct_map.names_to_uuids(_excluded_account_names())

        rows = manager.search_attachments(
            query,
            account=search_acct,
            mailbox=mailbox,
            limit=limit,
            exclude_mailboxes=exclude_mailboxes,
            exclude_accounts=list(excluded_uuids),
            before=before,
            after=after,
            offset=offset,
        )

        return _maybe_hint(
            [
                {
                    "id": row["message_id"],
                    "subject": row["subject"],
                    "sender": row["sender"],
                    "date_received": to_local_iso(row["date_received"]),
                    "score": 1.0,
                    "message_id": row["rfc822_message_id"],
                    "matched_in": f"attachment: {row['filename']}",
                    "account": acct_map.uuid_to_name(row["account"]),
                    "mailbox": row["mailbox"],
                }
                for row in rows
            ]
        )

    # S5: Split FTS5 vs JXA resolution
    # FTS5: None = search all accounts/mailboxes
    fts_account = account
    fts_mailbox = mailbox
    # JXA: resolve to a concrete, non-excluded target (never let a
    # None/excluded default implicitly scan a hidden account).
    jxa_account = await _resolve_visible_account(account)
    jxa_mailbox = _resolve_mailbox(mailbox)

    # Try FTS5 index for all searchable scopes
    if scope in ("all", "body", "subject", "sender"):
        manager = _get_index_manager()
        if manager.has_index():
            # Translate friendly name → UUID for index lookup
            acct_map = _get_account_map()
            await acct_map.ensure_loaded()

            search_account = None
            if fts_account:
                search_account = (
                    acct_map.name_to_uuid(fts_account)
                    or fts_account  # fallback: maybe already UUID
                )

            excluded_uuids = acct_map.names_to_uuids(_excluded_account_names())

            # Map scope to FTS5 column filter
            fts_column = None
            if scope == "subject":
                fts_column = "subject"
            elif scope == "sender":
                fts_column = "sender"

            try:
                results = manager.search(
                    query,
                    account=search_account,
                    mailbox=fts_mailbox,
                    limit=limit,
                    exclude_mailboxes=exclude_mailboxes,
                    exclude_accounts=list(excluded_uuids),
                    column=fts_column,
                    before=before,
                    after=after,
                    offset=offset,
                    highlight=highlight,
                )
            except Exception as e:
                err_msg = str(e) or repr(e)
                raise RuntimeError(
                    f"Search index error: {err_msg}. "
                    f"Try 'apple-mail-mcp rebuild' if this persists."
                ) from e
            return _maybe_hint(
                [
                    {
                        "id": r.id,
                        "subject": r.subject,
                        "sender": r.sender,
                        "date_received": to_local_iso(r.date_received),
                        "score": r.score,
                        "message_id": r.rfc822_message_id,
                        "matched_in": (
                            scope
                            if scope in ("subject", "sender")
                            else _detect_matched_columns(query, r)
                        ),
                        "content_snippet": r.content_snippet,
                        "account": acct_map.uuid_to_name(r.account),
                        "mailbox": r.mailbox,
                    }
                    for r in results
                ]
            )

    # Date filtering and highlight require the FTS5 index
    if before or after:
        raise ValueError(
            "Date filtering (before/after) requires the search "
            "index. Run 'apple-mail-mcp index' to build it."
        )

    # JXA-based search for subject/sender or when no index
    if _excluded_account_names() and (
        jxa_account is None or jxa_account in _excluded_account_names()
    ):
        # With exclusions active, a None target means no visible
        # account exists — JXA would scan Mail.accounts()[0], a hidden
        # one. (The name check is defense-in-depth; the resolver never
        # returns a hidden name.)
        return []

    safe_query_js = json.dumps(query.lower())

    if scope == "subject":
        filter_expr = (
            f"(data.subject[i] || '').toLowerCase().includes({safe_query_js})"
        )
    elif scope == "sender":
        filter_expr = (
            f"(data.sender[i] || '').toLowerCase().includes({safe_query_js})"
        )
    else:
        # "all" without index - search subject and sender
        filter_expr = f"""(
            (data.subject[i] || '').toLowerCase().includes({safe_query_js}) ||
            (data.sender[i] || '').toLowerCase().includes({safe_query_js})
        )"""

    q = (
        QueryBuilder()
        .from_mailbox(jxa_account, jxa_mailbox)
        .select("standard")
        .where(filter_expr)
        .order_by("date_received", descending=True)
        .limit(limit)
    )

    emails = await execute_query_async(q)

    # Convert to SearchResult format
    return _maybe_hint(
        [
            {
                "id": e["id"],
                "message_id": e.get("message_id"),
                "subject": e["subject"],
                "sender": e["sender"],
                "date_received": to_local_iso(e["date_received"]),
                "score": 1.0,  # No ranking for JXA search
                "matched_in": scope if scope != "all" else "metadata",
            }
            for e in emails
        ]
    )


@mcp.tool
async def set_flag(
    message_ids: int | str | list[int | str],
    color: Literal[
        "default",
        "none",
        "red",
        "orange",
        "yellow",
        "green",
        "blue",
        "purple",
        "gray",
    ] = "default",
    account: str | None = None,
    mailbox: str | None = None,
) -> WriteResult:
    """
    Flag or unflag one or more emails, optionally with a color.

    Write operation — refused when the server runs read-only
    (APPLE_MAIL_READ_ONLY / `serve -r`).

    Args:
        message_ids: One reference or a list of them (max 500 per call).
            Prefer the `message_id` field of a search / get_emails
            result — the RFC822 Message-ID header, e.g.
            "<a1b2@example.com>". It is the only handle that survives
            the message being filed elsewhere, which happens routinely
            when a phone or tablet is on the same account.
            The numeric `id` also works, but it is a per-mailbox ROWID:
            it is exact while the message stays put and dead as soon as
            it moves. Use it only when no `message_id` is available.
        color: What to set:
            - "default": flag without forcing a color (default)
            - "none": remove the flag
            - "red" | "orange" | "yellow" | "green" | "blue" |
              "purple" | "gray": flag with that color

            These are Apple Mail's seven colors and nothing more. This
            server attaches NO meaning to any of them and never will:
            what a color stands for is the user's own convention, it
            differs from person to person, and it belongs wherever they
            keep their instructions — not in this tool. Do not invent a
            scheme, and do not assume one; if you do not know what a
            color means to this user, ask them.
        account: Optional hint. Speeds id resolution; required (with
            mailbox) to place ids when no search index is built.
        mailbox: Optional hint (see account).

    Returns:
        A dict with per-reference outcome buckets so partial success is
        visible. Each reference is echoed exactly as it was passed:
        - updated: references actually changed
        - unchanged: already in the requested state, so no write was
          sent (still a success — treat as done)
        - not_found: could not be located (unknown, or deleted)
        - skipped_hidden: resolved into an excluded account
        - hint: guidance, present only when something is actionable

    Examples:
        >>> set_flag("<a1b2@example.com>", color="red")
        >>> set_flag(["<a@x.com>", "<b@x.com>"], color="orange")
        >>> set_flag("<a1b2@example.com>", color="none")  # unflag
        >>> set_flag(12345, color="red")  # numeric id, if that's all
    """
    _ensure_writable()

    if color == "none":
        flagged, flag_index = False, None
    elif color == "default":
        flagged, flag_index = True, None
    else:
        flagged, flag_index = True, FLAG_COLOR_INDEX[color]

    return await _apply_write(
        message_ids,
        account,
        mailbox,
        lambda groups: WriteBuilder.set_flag(
            groups, flagged=flagged, flag_index=flag_index
        ),
    )


@mcp.tool
async def set_read_status(
    message_ids: int | str | list[int | str],
    read: bool = True,
    account: str | None = None,
    mailbox: str | None = None,
) -> WriteResult:
    """
    Mark one or more emails read (seen) or unread (unseen).

    Write operation — refused when the server runs read-only
    (APPLE_MAIL_READ_ONLY / `serve -r`).

    Args:
        message_ids: One reference or a list of them (max 500 per call).
            Prefer the `message_id` field of a search / get_emails
            result — the RFC822 Message-ID header, e.g.
            "<a1b2@example.com>". It is the only handle that survives
            the message being filed elsewhere, which happens routinely
            when a phone or tablet is on the same account.
            The numeric `id` also works, but it is a per-mailbox ROWID:
            it is exact while the message stays put and dead as soon as
            it moves. Use it only when no `message_id` is available.
        read: True marks read/seen (default); False marks unread/unseen.
        account: Optional hint. Speeds id resolution; required (with
            mailbox) to place ids when no search index is built.
        mailbox: Optional hint (see account).

    Returns:
        A dict with per-reference outcome buckets so partial success is
        visible. Each reference is echoed exactly as it was passed:
        - updated: references actually changed
        - unchanged: already in the requested state, so no write was
          sent (still a success — treat as done)
        - not_found: could not be located (unknown, or deleted)
        - skipped_hidden: resolved into an excluded account
        - hint: guidance, present only when something is actionable

    Examples:
        >>> set_read_status("<a1b2@example.com>")  # mark read
        >>> set_read_status(["<a@x.com>", "<b@x.com>"], read=False)
        >>> set_read_status(12345)  # numeric id, if that's all there is
    """
    _ensure_writable()

    return await _apply_write(
        message_ids,
        account,
        mailbox,
        lambda groups: WriteBuilder.set_read(groups, read),
    )


@mcp.tool
async def refresh_index(full: bool = False) -> dict:
    """
    Update or completely rebuild THIS server's mail search index.

    Use this for any request to refresh, update, re-index, rebuild or
    recreate the mail index or mail search — including "rebuild the mail
    index from scratch" and its equivalents in other languages (e.g.
    German "bau den Mail-Index neu auf"). Pass full=True whenever the
    user says rebuild, from scratch, completely or similar.

    This is the FTS5 index this server maintains at
    ~/.apple-mail-mcp/index.db. It is NOT Apple Mail's own envelope
    index, and it has nothing to do with Mail.app's "Mailbox > Rebuild"
    menu item — never send the user there for this; you can do it here.

    The index otherwise syncs only when the server starts, so a
    long-running client drifts out of date. Also call it when
    `get_index_status` reports a large `staleness_hours`, or when a
    message the user just received cannot be found.

    This touches only the local index — never the mail itself — so it is
    allowed in read-only mode.

    Args:
        full: False (default) syncs changes since the last run — fast,
            returns when done. True discards the index and rebuilds from
            scratch; that takes minutes, so it runs in the background and
            returns immediately. Only use it when the index is suspected
            to be corrupt.

    Returns:
        Dict with `status` ("completed", "started", "already_running" or
        "failed"), a `message` to relay, and `changes` (added + deleted +
        moved) for a completed sync.
    """
    manager = _get_index_manager()

    from .index.manager import IndexBusyError

    if manager.is_building() or manager.write_lock_held():
        return {
            "status": "already_running",
            "message": (
                "A full index build is already running. Check "
                "get_index_status for progress."
            ),
        }

    # A full rebuild is far too slow to block an MCP call on, and so is
    # the first build of a large mailbox — run both detached.
    if full or not manager.has_usable_index():
        # Confirm the build actually begins before claiming it did.
        # Reporting "started" for a thread that died on the first line
        # is how a refused build looked like a running one: the status
        # then said "ready" forever and nothing explained the mismatch.
        started = threading.Event()
        outcome: list[BaseException] = []

        def _build() -> None:
            try:
                manager.build_from_disk(on_started=started.set)
            except BaseException as exc:
                outcome.append(exc)
                if isinstance(exc, IndexBusyError):
                    logger.info("Index build not started: %s", exc)
                else:
                    logger.warning(
                        "Background index build failed", exc_info=True
                    )
            finally:
                started.set()  # never leave the caller waiting

        threading.Thread(target=_build, daemon=True).start()
        confirmed = await asyncio.to_thread(started.wait, BUILD_START_TIMEOUT)

        if outcome:
            exc = outcome[0]
            if isinstance(exc, IndexBusyError):
                return {
                    "status": "already_running",
                    "message": (
                        "An index update is already running, so a rebuild "
                        "could not start. Ask for the index status to see "
                        "how far along it is, then try again."
                    ),
                }
            return {
                "status": "failed",
                "message": "The index rebuild could not be started.",
                "error": f"{type(exc).__name__}: {exc}",
            }

        if not confirmed:
            # The wait timed out and the thread has not failed either:
            # the build may be starting slowly, or it may be stuck. Not
            # knowing is not the same as "started" — say which one this
            # is, or a status tool showing no progress looks like a
            # second bug rather than the answer.
            return {
                "status": "unconfirmed",
                "message": (
                    "A rebuild was started but had not begun reading "
                    f"mail after {BUILD_START_TIMEOUT:.0f} seconds. Ask "
                    "for the index status in a minute: rising progress "
                    "means it is running, an unchanged index means it "
                    "is stuck and the server log has the reason."
                ),
            }

        return {
            "status": "started",
            "message": (
                "Building the index in the background. This can take "
                "several minutes on a large mailbox — ask for the index "
                "status to see progress."
            ),
        }

    try:
        changes = await asyncio.to_thread(manager.sync_updates)
    except IndexBusyError:
        return {
            "status": "already_running",
            "message": (
                "An index build or sync is already running. Ask for the "
                "index status to see how far along it is."
            ),
        }
    except Exception as exc:
        # A raw traceback is useless to the user this tool exists for.
        logger.warning("Index sync failed", exc_info=True)
        return {
            "status": "failed",
            "message": "The index could not be updated.",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if manager.last_error:
        # sync_updates reports 0 changes for a permission failure too,
        # so never present that as a successful refresh.
        _, _, next_steps, user_message = _index_guidance(
            state="ready",
            mail_dir_accessible=False,
            auto_build=get_index_auto_build_flag(),
        )
        return {
            "status": "failed",
            "message": user_message,
            "error": manager.last_error,
            "next_steps": next_steps,
        }

    return {
        "status": "completed",
        "changes": changes,
        "message": (
            f"Index updated: {changes} change(s)."
            if changes
            else "Index was already up to date."
        ),
    }


@mcp.tool
async def get_index_status() -> dict:
    """
    Diagnose the mail index: readiness, build progress, and setup
    problems — with step-by-step instructions to fix them.

    Call this whenever email tooling behaves unexpectedly, without
    waiting to be asked: search returns nothing, a write reports ids as
    not_found, or the user asks "is it working / how far along is it /
    why can't you find my mail". Reads state only; changes nothing.

    When the result contains `problem` or `next_steps`, do not just dump
    the JSON: tell the user what is wrong in their own language and walk
    them through the steps. Most users have never opened a terminal —
    `next_steps` is ordered and written for them, so follow it as given.

    Returns:
        Dict with, among others:
        - state: "building" | "ready" | "empty" | "absent"
        - user_message: one plain sentence to relay to the user
        - next_steps: ordered, non-technical instructions (may be empty)
        - problem / note: what's wrong, or why the setup is fine anyway
        - indexed_emails / disk_emails / progress_percent: build
          progress (counts rise continuously while a build runs)
        - mail_dir_accessible: False means macOS Full Disk Access is
          missing for the app running this server — the most common
          cause of an empty index
        - index_command: the exact command for *this* install, if one
          is needed
        - index_mode ("automatic"/"manual"), install_mode
          ("bundle"/"cli"), server_version, read_only,
          write_tools_enabled: setup and telemetry
        - recent_events: what the server actually did, newest first
          (build/sync started, finished, failed). This is the only
          diagnostic channel a desktop-extension user can reach — quote
          from it when explaining unexpected behaviour.
        - server_revision / source_ref / log_file: which build is
          answering, and where its log is
        - last_error, failed_parse_jobs, last_sync, staleness_hours,
          db_size_mb, excluded_accounts: health details
    """
    manager = _get_index_manager()

    # Probe Mail access directly: this is the single most common
    # failure (no Full Disk Access) and it must be reported even when
    # no index exists yet.
    mail_dir_accessible = True
    mail_dir_missing = False
    mail_dir: str | None = None

    def _probe_mail_dir() -> str:
        from .index.disk import find_mail_directory

        path = find_mail_directory()
        # find_mail_directory() caches for the life of the process, so
        # on its own it answers "was access granted at startup?". Access
        # can be revoked while the server runs, and detecting that is
        # what this probe is FOR — so read the directory for real.
        with os.scandir(path) as entries:
            next(iter(entries), None)
        return str(path)

    try:
        mail_dir = await asyncio.to_thread(_probe_mail_dir)
    except FileNotFoundError as exc:
        # No ~/Library/Mail at all. Diagnosing that as a missing
        # permission sends the user into System Settings to grant access
        # to something that does not exist — Mail has simply never been
        # set up on this Mac.
        mail_dir_accessible = False
        mail_dir_missing = True
        mail_dir = None
        logger.debug("Mail directory absent: %s", exc)
    except Exception as exc:
        mail_dir_accessible = False
        mail_dir = None
        logger.debug("Mail directory probe failed: %s", exc)

    building = manager.is_building()
    # A sync holds the same write lock but sets no build phase, so it
    # was completely invisible: counts and last_sync only move when it
    # finishes, which looks exactly like nothing happening.
    syncing = manager.write_lock_held() and not building
    has_index = manager.has_index()
    indexed = await asyncio.to_thread(manager.indexed_email_count)

    if building:
        state = "building"
    elif not has_index:
        state = "absent"
    elif indexed == 0:
        state = "empty"
    else:
        state = "ready"

    from .config import get_index_auto_build

    auto_build = get_index_auto_build()

    result: dict = {
        "state": state,
        "indexed_emails": indexed,
        "mail_dir_accessible": mail_dir_accessible,
        "mail_directory": mail_dir,
        "index_mode": "automatic" if auto_build else "manual",
        "sync_running": syncing,
        # fork-only:start — build identity of the bundle distribution
        "install_mode": _install_mode(),
        "server_revision": SERVER_REVISION,
        "source_ref": os.environ.get("APPLE_MAIL_MCP_REF") or "(default)",
        # fork-only:end
        "server_version": _server_version(),
        "log_file": str(_log_file_path()),
        # Whether the log is actually being WRITTEN — a client with no
        # filesystem access could see the path and nothing else, so
        # "logging is configured" and "logging works" looked the same.
        # The path and these two facts only; the contents stay on disk,
        # because log lines carry subjects and file paths.
        **_log_file_facts(),
        "read_only": get_read_only_mode(),
        "write_tools_enabled": not get_read_only_mode(),
        "index_command": _index_command(),
        "last_error": manager.last_error,
    }

    # While a build runs, report progress from the cached disk count:
    # the counts rise as batches commit, and this is exactly when the
    # user wants a percentage. A fresh disk walk would compete with the
    # build for I/O, so only the cached denominator is used here.
    if building:
        # Heartbeat first: it answers "working or wedged?", which the
        # raw count cannot when a slow mailbox is being parsed.
        progress = manager.build_progress()
        if progress is not None:
            result["build_phase"] = progress["phase"]
            result["build_emails_done"] = progress["emails_done"]
            result["build_files_seen"] = progress["files_seen"]
            result["seconds_since_progress"] = progress[
                "seconds_since_progress"
            ]
            result["build_appears_stalled"] = progress["appears_stalled"]
        cached_total = manager.cached_disk_count()
        if cached_total:
            result["disk_emails"] = cached_total
            result["progress_percent"] = round(
                min(100.0, 100.0 * indexed / cached_total), 1
            )

    # A sync competes for the same I/O as a build, so it gets the same
    # treatment: report from the cached count instead of walking.
    if syncing:
        cached_total = manager.cached_disk_count()
        if cached_total:
            result["disk_emails"] = cached_total

    # Richer stats need a disk walk; skip them when Mail is
    # unreachable (they'd only fail) or while a build or sync is
    # running (see above).
    if has_index and not building and not syncing and mail_dir_accessible:
        try:
            stats = await asyncio.to_thread(manager.get_stats)
            result.update(
                {
                    "disk_emails": stats.disk_email_count,
                    "mailboxes": stats.mailbox_count,
                    "attachments": stats.attachment_count,
                    "db_size_mb": round(stats.db_size_mb, 2),
                    "failed_parse_jobs": stats.failed_jobs_count,
                    # The count alone is a dead end: it says three
                    # messages are missing and not WHICH. Carry a few
                    # rows so the reason is actionable without reading
                    # the log file.
                    **(
                        {
                            "failed_parse_examples": [
                                {
                                    "path": j["emlx_path"],
                                    "mailbox": (
                                        f"{j['account']}/{j['mailbox']}"
                                    ),
                                    "reason": j["error_type"],
                                    "detail": (j["error_message"] or "")[:200],
                                }
                                for j in manager.failed_jobs(limit=5)
                            ]
                        }
                        if stats.failed_jobs_count
                        else {}
                    ),
                    "excluded_accounts": stats.excluded_accounts,
                    "last_sync": (
                        stats.last_sync.isoformat() if stats.last_sync else None
                    ),
                    "staleness_hours": (
                        round(stats.staleness_hours, 2)
                        if stats.staleness_hours is not None
                        else None
                    ),
                }
            )
            skipped = await asyncio.to_thread(manager.count_skipped_too_large)
            if skipped:
                result["skipped_too_large"] = skipped
                result["skipped_note"] = (
                    f"{skipped} message(s) exceed the size limit and are "
                    f"not searchable — this is why indexed_emails can be "
                    f"below disk_emails. Raise APPLE_MAIL_INDEX_MAX_EMAIL_MB "
                    f"(default 25) and rebuild to include them."
                )
            legacy = await asyncio.to_thread(manager.count_without_stable_id)
            result["without_stable_id"] = legacy
            if legacy:
                result["note"] = (
                    f"{legacy} indexed message(s) predate stable "
                    f"Message-ID tracking. If another device moves one, "
                    f"writes to it will fail with not_found. "
                    f"refresh_index(full=True) backfills them."
                )
            if stats.disk_email_count:
                pct = 100.0 * indexed / stats.disk_email_count
                result["progress_percent"] = round(min(pct, 100.0), 1)
        except Exception as exc:
            logger.debug("get_stats failed: %s", exc, exc_info=True)
            result["stats_error"] = str(exc)

    # Raw fields alone leave a non-technical user stranded: derive an
    # explicit diagnosis plus ordered, GUI-first steps the assistant can
    # read out verbatim.
    problem, note, next_steps, user_message = _index_guidance(
        state=state,
        mail_dir_accessible=mail_dir_accessible,
        mail_dir_missing=mail_dir_missing,
        auto_build=auto_build,
        stalled=bool(result.get("build_appears_stalled")),
        phase=result.get("build_phase"),
        syncing=syncing,
        error=manager.last_error,
    )
    if problem:
        result["problem"] = problem
    if note:
        result["note"] = note
    if next_steps:
        result["next_steps"] = next_steps
    result["user_message"] = user_message
    # The extension's stderr is unreachable, so this ring is the only
    # place a user can see what the server just did.
    result["recent_events"] = manager.recent_events()
    result["assistant_instructions"] = (
        "Relay `user_message` in the user's language, then walk them "
        "through `next_steps` one at a time. Assume no terminal "
        "experience: prefer the System Settings steps, and only offer a "
        "command if the steps include one — then give it verbatim in a "
        "code block and explain what it does. Do not tell the user to "
        "edit config files or environment variables."
    )
    return result


# ========== MCP Resources ==========


@mcp.resource(
    uri="index://status",
    name="IndexStatus",
    description=(
        "Read-only snapshot of the FTS5 search index: counts, size, "
        "last sync timestamp, and staleness in hours. Clients can use "
        "this to assess index health without invoking a tool."
    ),
    mime_type="application/json",
    tags={"index", "monitoring"},
)
async def index_status() -> str:
    """JSON snapshot of search-index health and counts."""
    manager = _get_index_manager()
    if not manager.has_index():
        return json.dumps(
            {
                "has_index": False,
                "message": (
                    "No index found. Run 'apple-mail-mcp index' to build it."
                ),
            }
        )

    # get_stats() walks ~/Library/Mail/V*/ for disk_email_count, which
    # can be slow on large mailboxes — push to a worker thread.
    stats = await asyncio.to_thread(manager.get_stats)

    return json.dumps(
        {
            "has_index": True,
            "email_count": stats.email_count,
            "mailbox_count": stats.mailbox_count,
            "attachment_count": stats.attachment_count,
            "disk_email_count": stats.disk_email_count,
            "db_size_mb": round(stats.db_size_mb, 2),
            "capped_mailboxes": stats.capped_mailboxes,
            "failed_jobs_count": stats.failed_jobs_count,
            "last_sync": (
                stats.last_sync.isoformat() if stats.last_sync else None
            ),
            "staleness_hours": (
                round(stats.staleness_hours, 2)
                if stats.staleness_hours is not None
                else None
            ),
            "excluded_accounts": stats.excluded_accounts,
        }
    )


if __name__ == "__main__":
    mcp.run()
