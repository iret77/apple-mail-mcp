"""
Apple Mail MCP Server

3-layer architecture for fast email access:
1. Disk-first reads — single emails via .emlx parsing (~3ms, no JXA)
2. FTS5 search — full-text body search in ~2ms with BM25 ranking
3. JXA fallback — batch property fetching for multi-email listing

TOOLS (8 total):
- list_accounts() - List email accounts
- list_mailboxes(account?) - List mailboxes
- get_emails(..., filter?) - Unified email listing with filters
- get_email(id) - Get single email with content (disk-first)
- search(query, ...) - Unified search with FTS5 support
- get_email_links(id) - Extract hyperlinks from an email
- get_email_attachment(id, filename) - Extract a file attachment
- get_attachment(id, filename?) - Deprecated alias

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
import time
from datetime import datetime
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


class EmailSummary(TypedDict):
    """Summary of an email (used in list/search results)."""

    id: int
    subject: str
    sender: str
    date_received: str
    read: bool
    flagged: bool


class SearchResult(TypedDict, total=False):
    """Result from search operations."""

    id: int
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
    reply_to: str
    message_id: str
    attachments: list[AttachmentSummary]


# ========== Write-Tool Helpers ==========

# Ceiling on one write batch. A batch is one osascript call per
# (account, mailbox) group, so the cost is in the ids, not the call;
# the cap exists so a runaway list cannot hold Mail.app hostage.
MAX_WRITE_BATCH = 500

# Located writes address a known mailbox directly, so they are fast;
# the all-mailbox scan reuses Strategy 3's budget.
WRITE_TIMEOUT = _clamped_env_int("APPLE_MAIL_WRITE_TIMEOUT", 30, 1, 300)


class WriteResult(TypedDict, total=False):
    """Per-id outcome of a batch write.

    Every id the caller passed appears in exactly one bucket, so a
    partial success is reportable rather than an exception.
    """

    updated: list[int]
    unchanged: list[int]
    not_found: list[int]
    skipped_hidden: list[int]
    failed: list[int]
    error: str
    hint: str
    diagnostics: dict


def _normalize_message_ids(
    message_ids: int | list[int],
) -> list[int]:
    """Coerce a single id or a list into a validated, unique list.

    Order is preserved so the answer reads in the order asked. A
    non-integer id is refused by name rather than silently dropped: a
    caller that mistyped one reference must not read the result as "that
    message does not exist".
    """
    raw = (
        list(message_ids)
        if isinstance(message_ids, (list, tuple))
        else [message_ids]
    )
    if not raw:
        raise ValueError("No message ids given.")
    if len(raw) > MAX_WRITE_BATCH:
        raise ValueError(
            f"Too many ids ({len(raw)}); max {MAX_WRITE_BATCH} per call."
        )
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            try:
                item = int(item)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid message id {item!r}: expected an integer."
                ) from None
        if item not in out:
            out.append(item)
    return out


async def _resolve_write_targets(
    ids: list[int],
    account: str | None,
    mailbox: str | None,
) -> tuple[list[dict], list[int], list[int]]:
    """Resolve message ids to JXA write groups, honoring the account gate.

    A Mail.app id is a per-mailbox ROWID, not a globally addressable
    handle: `Mail.messages.byId()` needs to know which mailbox to look
    in. So each id is placed first via the index's location resolver
    (the same machinery `get_email` Strategy 2 leans on), then via an
    explicit `account` + `mailbox` hint (which JXA verifies), and
    failing both via a bounded all-mailbox **scan** of a visible account
    — mirroring `get_email` Strategy 3, so writes work with no index at
    all.

    An id that resolves into an excluded account (#90) goes to
    `skipped_hidden` and is never dispatched to JXA. Only ids with no
    visible account to scan land in `not_found`.

    Returns ``(groups, not_found, skipped_hidden)``. Located groups are
    ``{"account", "mailbox", "ids"}``; the optional scan group is
    ``{"account", "ids", "scan": True}``.
    """
    # Explicit hidden account: refuse the whole batch up front, exactly
    # as the read tools do at their entry gate.
    if _hidden_account(account):
        return [], [], list(ids)

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

    # Fallback target for ids the index cannot place: usable only when
    # the caller pinned BOTH account and mailbox (the account is known
    # non-hidden here — the explicit-hidden case returned above).
    hint_location: tuple[str, str] | None = (
        (account, mailbox) if account and mailbox else None
    )

    grouped: dict[tuple[str, str], list[int]] = {}
    scan_ids: list[int] = []
    not_found: list[int] = []
    skipped_hidden: list[int] = []

    for mid in ids:
        located: tuple[str, str] | None = None
        if has_index:
            loc = manager.find_email_location(
                mid, account=idx_acct_uuid, mailbox=mailbox
            )
            if loc:
                acct_uuid, mb_name = loc
                if acct_uuid in excluded_uuids:
                    skipped_hidden.append(mid)
                    continue
                located = (acct_map.uuid_to_name(acct_uuid), mb_name)
        if located is None and hint_location is not None:
            located = hint_location
        if located is None:
            # No index hit, no hint: defer to the bounded JXA scan.
            scan_ids.append(mid)
            continue
        grouped.setdefault(located, []).append(mid)

    groups: list[dict] = [
        {"account": acct, "mailbox": mb, "ids": mids}
        for (acct, mb), mids in grouped.items()
    ]

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

    return groups, not_found, skipped_hidden


def _absorb_failures(res: dict, failed: list, errors: list) -> None:
    """Move JXA-reported failures out of the caller's success path.

    The write script distinguishes "Mail was open and the message was
    not there" (`not_found`) from "we never got that far" (`failures`,
    each with its reason). Merging the two would make a broken account
    or mailbox lookup look like a batch of deleted mail.
    """
    for item in res.get("failures", []) or []:
        target = item.get("target")
        if target is None:
            continue
        failed.append(int(target))
        reason = str(item.get("reason", "")).strip()
        if reason:
            errors.append(reason)


async def _apply_write(
    message_ids: int | list[int],
    account: str | None,
    mailbox: str | None,
    make_builder,
) -> WriteResult:
    """Shared orchestration for the batch write tools.

    Normalizes ids, resolves targets (with the account gate), then runs
    the located groups and any scan group in *separate* osascript calls
    — so a slow or timed-out mailbox scan cannot discard the fast,
    precise located writes — and merges every id's outcome.
    `make_builder` maps ``groups -> WriteBuilder``: the only per-tool
    difference.

    Every id comes back in exactly one bucket.
    """
    ids = _normalize_message_ids(message_ids)
    groups, not_found, skipped_hidden = await _resolve_write_targets(
        ids, account, mailbox
    )

    located = [g for g in groups if not g.get("scan")]
    scan = [g for g in groups if g.get("scan")]
    updated: list[int] = []
    unchanged: list[int] = []
    # A write that never reached Apple Mail is NOT evidence that the
    # message is gone. Reporting it as not_found would send the caller
    # hunting for a message that was there all along.
    failed: list[int] = []
    errors: list[str] = []
    # Mailboxes the scan never reached: a miss that follows one of these
    # is not evidence that the message is gone.
    unsearched = 0

    def _merge(res: dict) -> None:
        nonlocal unsearched
        updated.extend(int(x) for x in res.get("updated", []))
        unchanged.extend(int(x) for x in res.get("unchanged", []))
        not_found.extend(int(x) for x in res.get("not_found", []))
        _absorb_failures(res, failed, errors)
        unsearched += int(res.get("scan_capped") or 0) + int(
            res.get("scan_unreadable") or 0
        )

    if located:
        try:
            _merge(
                await execute_with_core_async(
                    make_builder(located).build(), timeout=WRITE_TIMEOUT
                )
            )
        except Exception as exc:
            # The contract is that every id lands in exactly one bucket.
            # Letting this raise would leave the caller with no idea
            # which writes did or did not happen.
            logger.warning("located write failed: %s", exc, exc_info=True)
            failed += [i for g in located for i in g["ids"]]
            errors.append(str(exc))

    if scan:
        builder = make_builder(scan)
        builder.max_scan_mailboxes = STRATEGY3_MAX_MAILBOXES
        try:
            _merge(
                await execute_with_core_async(
                    builder.build(), timeout=STRATEGY3_TIMEOUT
                )
            )
        except Exception as exc:
            logger.warning("write scan failed: %s", exc, exc_info=True)
            failed += [i for g in scan for i in g["ids"]]
            errors.append(str(exc))

    result: WriteResult = {
        "updated": updated,
        "unchanged": unchanged,
        "not_found": not_found,
        "skipped_hidden": skipped_hidden,
    }
    if not_found or failed:
        # Say what was actually attempted. A bare not_found cannot be
        # checked by the caller: it looks the same whether the index
        # placed the message or the search never reached it.
        result["diagnostics"] = {
            "located_by_index": [
                f"{g['account']}/{g['mailbox']}" for g in located
            ],
            "accounts_scanned": [g.get("account") for g in scan],
            "mailboxes_not_searched": unsearched,
        }
    if failed:
        # Say plainly that Apple Mail never carried the write out, and
        # what it said — the caller must not read this as "deleted".
        result["failed"] = failed
        result["error"] = "; ".join(dict.fromkeys(errors))[:500]
        blob = result["error"].lower()
        if "no such account" in blob:
            cause = (
                "The account name taken from the index does not match any "
                "account in Mail. Re-index and retry; passing `account` "
                "with the name Mail shows also works."
            )
        elif "cannot open mailbox" in blob or "cannot list mailboxes" in blob:
            cause = (
                "The mailbox could not be opened under that name — the "
                "index may name it differently than Mail does. Pass "
                "`account` and `mailbox` as Mail shows them."
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
        result["hint"] = (
            f"{len(failed)} write(s) never reached the message — this is "
            f"NOT a statement about the mail, which is most likely fine. "
            f"Reported: {result['error']} — {cause} Reads keep working "
            f"meanwhile because they come from the index and the .emlx "
            f"files, not from Apple Events."
        )
        return result
    if not_found and unsearched:
        result["hint"] = (
            f"{len(not_found)} id(s) were not found, but {unsearched} "
            f"mailbox(es) were never searched (scan limit, or a mailbox "
            f"Mail refused to read). This is NOT evidence that the "
            f"messages are gone — pass `account` and `mailbox` to aim the "
            f"write."
        )
    elif not_found and not _get_index_manager().has_index():
        result["hint"] = (
            "Some ids were not found by scanning the default account. "
            "Pass both `account` and `mailbox` for reliable resolution, "
            "or build the index (`apple-mail-mcp index`) so id lookup is "
            "exact."
        )
    elif not_found:
        # No claim of deletion. A Mail.app id is a per-mailbox ROWID, so
        # it cannot be searched for across accounts at all: the same
        # number is a different message elsewhere, and widening the
        # search would risk writing to a stranger's mail. The honest
        # answer is to state the limit.
        result["hint"] = (
            f"{len(not_found)} id(s) were not found where the index "
            f"expected them. A Mail.app id is only unique within its "
            f"mailbox, so it cannot be searched for in other accounts. "
            f"The message may have been filed elsewhere (from another "
            f"device), in which case its id has changed: re-index and "
            f"look it up again."
        )
    return result


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


# ========== MCP Tools (8 total) ==========


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
        account: Account name. Uses APPLE_MAIL_DEFAULT_ACCOUNT env var or
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

    Returns:
        List of email dictionaries sorted by date (newest first).

    Examples:
        >>> get_emails()  # All emails from default mailbox
        >>> get_emails(filter="unread", limit=10)  # Unread emails
        >>> get_emails("Work", "INBOX", filter="today")  # Today's work emails
    """
    limit, _ = _validate_pagination(limit)
    if _hidden_account(account):
        # Hidden account explicitly requested: return nothing and do
        # NOT fall through to JXA (which would surface its mail).
        return []
    # Resolve None/excluded-default to a visible account so neither the
    # fast path nor the JXA fallback implicitly targets a hidden one.
    target_account = await _resolve_visible_account(account)
    if target_account is None and _excluded_account_names():
        # No visible account at all (every account is hidden): the JXA
        # fallback would target Mail.accounts()[0] — a hidden one.
        return []
    target_mailbox = _resolve_mailbox(mailbox)

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
            if target_account:
                account_uuid = _get_account_map().name_to_uuid(target_account)
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

            if target_account and account_uuid is None:
                # Unknown account name. Fall through to JXA, which
                # reports it properly, rather than silently widening
                # the query to every account.
                logger.debug(
                    "Account %r not in AccountMap; falling back to JXA",
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
                )
                return [
                    EmailSummary(
                        id=r.message_id,
                        subject=r.subject,
                        sender=r.sender,
                        date_received=r.date_received,
                        read=r.read,
                        flagged=r.flagged,
                    )
                    for r in rows
                    # Belt-and-suspenders: drop any hidden-account rows
                    # that slip through an unscoped (cold-cache) query.
                    if r.account_uuid not in excluded_uuids
                ]
    except (
        FileNotFoundError,
        sqlite3.OperationalError,
        MailboxNotFoundError,
    ) as exc:
        logger.debug(
            "Envelope Index fast path unavailable (%s); falling back to JXA",
            exc,
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
        return await execute_query_async(query)
    except Exception as exc:
        # An unknown mailbox makes JXA fail with a raw "...Error:
        # Error: Can't get object. (-1728)". Surface a clean,
        # model-friendly message; re-raise other failures intact.
        msg = str(exc).lower()
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
    reply_to: msg.replyTo(),
    message_id: msg.messageId(),
    attachments: attachments
}});
"""


@mcp.tool
async def get_email(
    message_id: int,
    account: str | None = None,
    mailbox: str | None = None,
) -> EmailFull:
    """
    Get a single email with full content.

    Looks up the email across all accounts using a cascade strategy.
    Just pass the message_id — account/mailbox are optional hints
    that speed up lookup but are not required.

    Args:
        message_id: The email's unique ID (from search results)
        account: Optional hint (speeds up lookup, not required)
        mailbox: Optional hint (speeds up lookup, not required)

    Returns:
        Email dictionary with full content including:
        - id, subject, sender, date_received, date_sent
        - content: Full plain text body
        - read, flagged status
        - reply_to, message_id (email Message-ID header)
        - attachments: List of {filename, mime_type, size}

    Note:
        The attachments list comes from JXA's mailAttachments(),
        which only reports file attachments visible in Mail.app's
        UI. Inline images, S/MIME signatures, and attachments in
        sent/bounce-back emails may not appear. Use get_attachment
        with a known filename for reliable extraction from disk.

    Example:
        >>> get_email(12345)
        {"id": 12345, "subject": "Meeting notes",
         "content": "Hi team,\\n\\nHere are the notes...", ...}
    """
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

    def _enrich_attachments(result: dict) -> dict:
        """Replace JXA attachments with richer index data when available."""
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
                        return _enrich_attachments(result)
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

    # Stale-entry handling: clean up the dead row and fail fast with a
    # clear message. Skipping Strategies 1-3 here is intentional — they
    # would also fail (the message is gone from Mail.app), with Strategy 3
    # eating its full timeout before doing so.
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
        raise ValueError(
            f"Message {message_id} was deleted or moved since the last "
            f"index sync. Run 'apple-mail-mcp rebuild' to refresh "
            f"the index."
        )

    # Strategy 1: Try specified mailbox
    mailbox_setup = build_mailbox_setup_js(resolved_account, resolved_mailbox)
    script = _build_get_email_script(message_id, mailbox_setup)

    try:
        result = await execute_with_core_async(script)
        return _enrich_attachments(result)
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
                    return _enrich_attachments(result)
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
        return _enrich_attachments(result)
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
        msg = str(exc).lower()
        if "not found" in msg or "-1728" in msg or "can't get object" in msg:
            raise ValueError(f"Message {message_id} not found.") from None
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
    message_id: int,
    account: str | None = None,
    mailbox: str | None = None,
) -> _Path:
    """Resolve a message ID to an .emlx file path via the index.

    Raises:
        ValueError: If the index is missing or email not found.
    """
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


@mcp.tool
async def get_email_links(
    message_id: int,
    account: str | None = None,
    mailbox: str | None = None,
) -> dict:
    """
    Extract hyperlinks from an email's HTML content.

    Filters out mailto:, javascript:, and long tracking URLs.
    Returns deduplicated links with their anchor text.

    Requires the search index.

    Args:
        message_id: The email's unique ID
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
    message_id: int,
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
        message_id: The email's unique ID
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
    message_id: int,
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
        message_id: The email's unique ID
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
        List of matching emails with id, subject, sender,
        date_received, score, matched_in, content_snippet,
        account, and mailbox fields.

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
        "check spelling, or use scope='all' to search everywhere."
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
                    "date_received": row["date_received"],
                    "score": 1.0,
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
                        "date_received": r.date_received,
                        "score": r.score,
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
                "subject": e["subject"],
                "sender": e["sender"],
                "date_received": e["date_received"],
                "score": 1.0,  # No ranking for JXA search
                "matched_in": scope if scope != "all" else "metadata",
            }
            for e in emails
        ]
    )


@mcp.tool
async def set_flag(
    message_ids: int | list[int],
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
    Flag or unflag one or many messages, optionally in a given color.

    Args:
        message_ids: One id, or a list of them (max 500). A batch is one
            osascript call per (account, mailbox) group rather than one
            per message.
        color: "default" flags without forcing a color, "none" unflags,
            and the seven names set that flag color. The server attaches
            NO meaning to any color — what a color stands for is the
            user's own convention, so ask rather than assume.
        account: Optional hint. Speeds up resolution and is required
            (together with `mailbox`) for ids the index cannot place.
        mailbox: Optional hint, as above.

    Returns:
        Per-id buckets: `updated`, `unchanged` (already in that state),
        `not_found`, `skipped_hidden` (ids in an excluded account), and
        on trouble `failed` + `error` + `hint`. A batch never fails as a
        whole — every id you passed appears in exactly one bucket.

        `unchanged` is not a failure: the state was already what you
        asked for, and a no-op write is a server round-trip on
        IMAP/Exchange accounts, so it is deliberately skipped.

    Example:
        >>> set_flag(12345, color="red")
        {"updated": [12345], "unchanged": [], "not_found": [], ...}
        >>> set_flag([1, 2, 3], color="none")  # unflag a batch
    """
    _ensure_writable()

    if color == "none":
        flagged, flag_index = False, None
    elif color == "default":
        flagged, flag_index = True, None
    else:
        flagged, flag_index = True, FLAG_COLOR_INDEX[color]

    def make_builder(groups: list[dict]) -> WriteBuilder:
        return WriteBuilder.set_flag(groups, flagged, flag_index)

    return await _apply_write(message_ids, account, mailbox, make_builder)


@mcp.tool
async def set_read_status(
    message_ids: int | list[int],
    read: bool = True,
    account: str | None = None,
    mailbox: str | None = None,
) -> WriteResult:
    """
    Mark one or many messages as read (seen) or unread (unseen).

    Args:
        message_ids: One id, or a list of them (max 500).
        read: True marks as read (default), False as unread.
        account: Optional hint. Speeds up resolution and is required
            (together with `mailbox`) for ids the index cannot place.
        mailbox: Optional hint, as above.

    Returns:
        Per-id buckets, exactly as `set_flag` — see there. A batch never
        fails as a whole.

    Example:
        >>> set_read_status([1, 2, 3])  # mark read
        >>> set_read_status(12345, read=False)  # back to unread
    """
    _ensure_writable()

    def make_builder(groups: list[dict]) -> WriteBuilder:
        return WriteBuilder.set_read(groups, read)

    return await _apply_write(message_ids, account, mailbox, make_builder)


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
