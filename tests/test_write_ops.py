"""Tests for the write tools: set_flag and set_read_status.

JXA execution is mocked throughout, so these run without macOS or
Mail.app: what is under test is target resolution, the per-id bucket
contract and the read-only gate — not Apple Events.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_index(location=None, has_index=True):
    """An IndexManager double that resolves every id to `location`."""
    mgr = MagicMock()
    mgr.has_index.return_value = has_index
    mgr.find_email_location.return_value = location
    mgr.count_email_locations.return_value = 1 if location else 0
    return mgr


def _mock_acct_map(uuid_to_name="Work", excluded_uuids=None):
    m = MagicMock()
    m.ensure_loaded = AsyncMock()
    m.names_to_uuids.return_value = set(excluded_uuids or [])
    m.name_to_uuid.return_value = None
    m.uuid_to_name.return_value = uuid_to_name
    return m


class TestBuilderScript:
    """The generated JXA, without running it."""

    def test_ids_and_names_cross_over_as_json_only(self):
        """Nothing caller-supplied may be interpolated into executable
        JS: a mailbox called `"); doSomething();` must stay data."""
        from apple_mail_mcp.builders import WriteBuilder

        hostile = '"); throw new Error("pwned"); //'
        script = WriteBuilder.set_read(
            [{"account": hostile, "mailbox": hostile, "ids": [1]}], True
        ).build()

        assert json.dumps(hostile) in script
        assert 'throw new Error("pwned")' not in script.replace(
            json.dumps(hostile), ""
        )

    def test_flag_color_forces_the_index(self):
        from apple_mail_mcp.builders import FLAG_COLOR_INDEX, WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "A", "mailbox": "INBOX", "ids": [1]}],
            True,
            FLAG_COLOR_INDEX["red"],
        ).build()

        assert "msg.flaggedStatus = true; msg.flagIndex = 0;" in script

    def test_unflag_clears_rather_than_setting_a_color(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "A", "mailbox": "INBOX", "ids": [1]}], False
        ).build()

        assert "msg.flaggedStatus = false;" in script
        assert "flagIndex" not in script.split("JSON.stringify")[0].split(
            "needs"
        )[0].replace("msg.flaggedStatus() !== false", "")

    def test_a_no_op_is_skipped_by_reading_live_state(self):
        """Every write is a server round-trip on IMAP/Exchange, so a
        write that changes nothing is not free — and the current state
        must come from Mail, never from a possibly-stale index."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "mailbox": "INBOX", "ids": [1]}], True
        ).build()

        assert "msg.readStatus() !== true" in script
        assert 'return "unchanged"' in script

    def test_the_write_verifies_identity_before_writing(self):
        """The id array is a snapshot: if mail arrives or is moved
        between fetching it and writing, positions shift and the same
        index points at a different message."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "mailbox": "INBOX", "ids": [1]}], True
        ).build()

        assert "if (msg.id() !== targetId)" in script
        assert "collection.byId(targetId)" in script

    def test_the_scan_counts_what_it_left_out(self):
        """A scan that stopped early is not evidence of absence."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "ids": [1], "scan": True}], True
        ).build()

        assert "cappedBoxes" in script
        assert "unreadableBoxes" in script
        assert "scan_capped" in script
        assert "scan_unreadable" in script

    def test_environment_failures_are_kept_apart_from_not_found(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "mailbox": "INBOX", "ids": [1]}], True
        ).build()

        assert "no such account: " in script
        assert "cannot open mailbox " in script
        assert "failures: failures" in script


class TestTargetResolution:
    """Where a write is sent, and why."""

    @pytest.mark.asyncio
    async def test_the_index_places_the_id(self):
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = _mock_index(location=("uuid-work", "Archive"))
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
        ):
            groups, not_found, hidden, _amb = await _resolve_write_targets(
                [7], None, None
            )

        assert groups == [{"account": "Work", "mailbox": "Archive", "ids": [7]}]
        assert not not_found and not hidden

    @pytest.mark.asyncio
    async def test_an_explicit_hint_is_used_when_the_index_misses(self):
        from apple_mail_mcp.server import _resolve_write_targets

        with (
            patch(
                "apple_mail_mcp.server._get_index_manager",
                return_value=_mock_index(location=None),
            ),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
        ):
            groups, _, _, _amb = await _resolve_write_targets(
                [7], "Work", "Sent"
            )

        assert groups == [{"account": "Work", "mailbox": "Sent", "ids": [7]}]

    @pytest.mark.asyncio
    async def test_no_index_and_no_hint_falls_back_to_a_scan(self):
        """Writes have to work with no index at all — mirroring
        get_email's Strategy 3."""
        from apple_mail_mcp.server import _resolve_write_targets

        with (
            patch(
                "apple_mail_mcp.server._get_index_manager",
                return_value=_mock_index(has_index=False),
            ),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
        ):
            groups, not_found, _, _amb = await _resolve_write_targets(
                [7], None, None
            )

        assert groups == [{"account": "Work", "ids": [7], "scan": True}]
        assert not not_found

    @pytest.mark.asyncio
    async def test_ids_are_grouped_per_mailbox(self):
        """One osascript call per (account, mailbox), not per message."""
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = lambda mid, **kw: {
            1: ("uuid-work", "INBOX"),
            2: ("uuid-work", "INBOX"),
            3: ("uuid-work", "Sent"),
        }[mid]
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
        ):
            groups, _, _, _amb = await _resolve_write_targets(
                [1, 2, 3], None, None
            )

        assert sorted(len(g["ids"]) for g in groups) == [1, 2]


class TestExcludedAccountBoundary:
    """Hidden accounts (#90) must never reach JXA."""

    @pytest.mark.asyncio
    async def test_an_id_in_a_hidden_account_is_skipped(self):
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = _mock_index(location=("uuid-secret", "INBOX"))
        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(excluded_uuids={"uuid-secret"}),
            ),
        ):
            groups, not_found, hidden, _amb = await _resolve_write_targets(
                [7], None, None
            )

        assert groups == []
        assert hidden == [7]

    @pytest.mark.asyncio
    async def test_naming_a_hidden_account_skips_the_whole_batch(self):
        from apple_mail_mcp.server import _resolve_write_targets

        with patch(
            "apple_mail_mcp.server._excluded_account_names",
            return_value={"Secret"},
        ):
            groups, not_found, hidden, _amb = await _resolve_write_targets(
                [1, 2], "Secret", None
            )

        assert groups == []
        assert hidden == [1, 2]


class TestBucketContract:
    """Every id lands in exactly one bucket; a batch never fails whole."""

    async def _run(self, res, ids=(1,), **kw):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        with (
            patch(
                "apple_mail_mcp.server._get_index_manager",
                return_value=_mock_index(location=("uuid-work", "INBOX")),
            ),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                **res,
            ),
        ):
            return await _apply_write(
                list(ids),
                kw.get("account"),
                kw.get("mailbox"),
                lambda g: WriteBuilder.set_read(g, True),
            )

    @pytest.mark.asyncio
    async def test_updated_and_unchanged_are_reported_separately(self):
        out = await self._run(
            {
                "return_value": {
                    "updated": [1],
                    "unchanged": [2],
                    "not_found": [],
                }
            },
            ids=(1, 2),
        )

        assert out["updated"] == [1]
        assert out["unchanged"] == [2]

    @pytest.mark.asyncio
    async def test_a_reported_failure_is_not_a_missing_message(self):
        """ "not found" must stay a statement about the message. A
        broken account lookup is a statement about the environment."""
        out = await self._run(
            {
                "return_value": {
                    "updated": [],
                    "not_found": [],
                    "failures": [
                        {"target": 1, "reason": "no such account: Work"}
                    ],
                }
            }
        )

        assert out["failed"] == [1]
        assert out["not_found"] == []
        assert "no such account" in out["error"]
        assert "NOT a statement about the mail" in out["hint"]

    @pytest.mark.asyncio
    async def test_a_crashing_osascript_still_answers_per_id(self):
        out = await self._run({"side_effect": RuntimeError("osascript died")})

        assert out["failed"] == [1]
        assert "osascript died" in out["error"]

    @pytest.mark.asyncio
    async def test_a_capped_scan_is_not_reported_as_deletion(self):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        with (
            patch(
                "apple_mail_mcp.server._get_index_manager",
                return_value=_mock_index(has_index=False),
            ),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                return_value={
                    "updated": [],
                    "not_found": [7],
                    "scan_capped": 12,
                },
            ),
        ):
            out = await _apply_write(
                [7], None, None, lambda g: WriteBuilder.set_read(g, True)
            )

        assert out["not_found"] == [7]
        assert out["diagnostics"]["mailboxes_not_searched"] == 12
        assert "NOT evidence" in out["hint"]
        assert "deleted" not in out["hint"].lower()

    @pytest.mark.asyncio
    async def test_the_located_writes_survive_a_failing_scan(self):
        """Located and scan groups run in SEPARATE osascript calls, so a
        slow scan cannot discard the fast, precise writes."""
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = lambda mid, **kw: (
            ("uuid-work", "INBOX") if mid == 1 else None
        )
        calls = []

        async def fake(script, timeout=None):
            calls.append(script)
            if len(calls) == 1:
                return {"updated": [1], "not_found": []}
            raise TimeoutError("scan took too long")

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake,
            ),
        ):
            out = await _apply_write(
                [1, 2], None, None, lambda g: WriteBuilder.set_read(g, True)
            )

        assert len(calls) == 2
        assert out["updated"] == [1]
        assert out["failed"] == [2]


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_a_single_reference_and_a_list_are_both_accepted(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids(5) == [5]
        assert _normalize_message_ids([5, 6]) == [5, 6]
        assert _normalize_message_ids("<a@b>") == ["<a@b>"]

    @pytest.mark.asyncio
    async def test_duplicates_collapse_and_order_survives(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids([9, 3, 9]) == [9, 3]

    @pytest.mark.asyncio
    async def test_a_reference_that_cannot_be_one_is_named(self):
        """Silently dropping it would let the caller read the result as
        "that message does not exist". A Message-ID contains no
        whitespace, so this cannot be one."""
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="not a usable"):
            _normalize_message_ids([1, "not an id at all"])

    @pytest.mark.asyncio
    async def test_an_oversized_batch_is_refused(self):
        from apple_mail_mcp.server import (
            MAX_WRITE_BATCH,
            _normalize_message_ids,
        )

        with pytest.raises(ValueError, match="Too many"):
            _normalize_message_ids(list(range(MAX_WRITE_BATCH + 1)))


class TestReadOnlyMode:
    """`APPLE_MAIL_READ_ONLY` finally has something to guard (#80)."""

    @pytest.mark.asyncio
    async def test_set_flag_refuses(self):
        from apple_mail_mcp.config import set_read_only_mode
        from apple_mail_mcp.server import set_flag

        set_read_only_mode(True)
        try:
            with pytest.raises(PermissionError, match="read-only"):
                await set_flag(1)
        finally:
            set_read_only_mode(False)

    @pytest.mark.asyncio
    async def test_set_read_status_refuses(self):
        from apple_mail_mcp.config import set_read_only_mode
        from apple_mail_mcp.server import set_read_status

        set_read_only_mode(True)
        try:
            with pytest.raises(PermissionError, match="read-only"):
                await set_read_status(1)
        finally:
            set_read_only_mode(False)


class TestHeaderIsAFirstClassReference:
    """The RFC822 Message-ID as a write reference.

    A Mail.app id is a per-mailbox ROWID: it dies the moment any device
    files the message elsewhere, which is the normal case with a phone
    and a tablet on the same account. The header survives that.
    """

    @pytest.mark.asyncio
    async def test_a_header_becomes_a_header_group(self):
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = [("uuid-work", "Archive", 42)]
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Work"]),
            ),
        ):
            groups, not_found, hidden, _amb = await _resolve_write_targets(
                ["<a@x>"], None, None
            )

        assert groups[0]["by_header"] is True
        assert groups[0]["headers"] == ["<a@x>"]
        # The row is where it WAS: used to order the search, not to
        # restrict it, and never translated into a ROWID to write to.
        assert groups[0]["prefer_mailboxes"] == ["Archive"]
        assert not any("ids" in g for g in groups)

    @pytest.mark.asyncio
    async def test_the_indexed_account_is_searched_first_then_the_rest(self):
        """A row can be stale. A miss in the account it names used to end
        the search in silence while the message sat one account over."""
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 42)]
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Alpha", "Work", "Zeta"]),
            ),
        ):
            groups, _, _, _amb = await _resolve_write_targets(
                ["<a@x>"], None, None
            )

        # Insertion order, not alphabetical: sorting would throw the
        # index's priority away.
        assert [g["account"] for g in groups] == ["Work", "Alpha", "Zeta"]

    @pytest.mark.asyncio
    async def test_an_unplaceable_header_searches_every_account(self):
        """Not exotic: it is every message that arrived after the last
        sync. Scoped to one account, such a message is unreachable."""
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Work", "Private"]),
            ),
        ):
            groups, not_found, _, _amb = await _resolve_write_targets(
                ["<new@x>"], None, None
            )

        assert [g["account"] for g in groups] == ["Work", "Private"]
        assert not not_found

    @pytest.mark.asyncio
    async def test_a_header_only_in_a_hidden_account_is_skipped(self):
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = [("uuid-secret", "INBOX", 42)]
        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(excluded_uuids={"uuid-secret"}),
            ),
        ):
            groups, _, hidden, _amb = await _resolve_write_targets(
                ["<a@x>"], None, None
            )

        assert groups == []
        assert hidden == ["<a@x>"]

    @pytest.mark.asyncio
    async def test_headers_are_echoed_back_as_headers(self):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Work"]),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                return_value={"updated": ["<a@x>"], "not_found": []},
            ),
        ):
            out = await _apply_write(
                ["<a@x>"], None, None, lambda g: WriteBuilder.set_read(g, True)
            )

        assert out["updated"] == ["<a@x>"]

    @pytest.mark.asyncio
    async def test_a_missing_header_may_be_called_deleted_only_when_complete(
        self,
    ):
        """Every visible account was searched and nothing was skipped —
        the one case where absence has actually been established."""
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Work"]),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                return_value={
                    "updated": [],
                    "not_found": ["<gone@x>"],
                    "scan_capped": 0,
                    "scan_unreadable": 0,
                    "scan_skipped_discard": 0,
                },
            ),
        ):
            out = await _apply_write(
                ["<gone@x>"],
                None,
                None,
                lambda g: WriteBuilder.set_read(g, True),
            )

        assert "likely deleted" in out["hint"]

    @pytest.mark.asyncio
    async def test_a_skipped_discard_mailbox_forbids_that_claim(self):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["Work"]),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                return_value={
                    "updated": [],
                    "not_found": ["<maybe@x>"],
                    "scan_skipped_discard": 2,
                },
            ),
        ):
            out = await _apply_write(
                ["<maybe@x>"],
                None,
                None,
                lambda g: WriteBuilder.set_read(g, True),
            )

        assert "NOT evidence" in out["hint"]
        assert "deleted" not in out["hint"].lower()


class TestHeaderNormalization:
    """Angle brackets are not part of the identity."""

    def test_the_comparison_key_ignores_brackets(self):
        from apple_mail_mcp.server import _header_key

        assert _header_key("<a@b>") == _header_key("a@b")
        assert _header_key(None) == ""

    def test_the_builder_normalizes_on_both_sides(self):
        """The .emlx header keeps its brackets, Apple's messageId drops
        them. A strict comparison never matches."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "headers": ["<a@b>"], "by_header": True}], True
        ).build()

        assert "function normHeader(" in script
        assert "normHeader(msg.messageId()) !== normHeader(targetHeader)" in (
            script
        )

    def test_a_stringified_list_is_unwrapped(self):
        """Some clients serialize a list parameter as JSON text. Taken
        literally that is a Message-ID nothing will ever match, and the
        caller gets a mute not_found for a message sitting right there."""
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids('["<a@b>", "<c@d>"]') == [
            "<a@b>",
            "<c@d>",
        ]

    def test_html_escaped_brackets_are_decoded(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("&lt;a@b&gt;") == ["<a@b>"]

    def test_an_escaped_ampersand_is_left_alone(self):
        """`&` is legal in a Message-ID local part, so "<a&amp;b@x>" and
        "<a&b@x>" can both exist as distinct messages. Decoding would aim
        the write at the other one while reporting success."""
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("<a&amp;b@x>") == ["<a&amp;b@x>"]

    def test_a_settled_header_is_not_reported_missing_by_another_account(
        self,
    ):
        """One header goes to several account groups. Retiring it
        globally is what stops a double write and a mute miss."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "headers": ["<a@b>"], "by_header": True}], True
        ).build()

        assert "const settled = new Set();" in script
        assert "settled.has(String(t))" in script


class TestHeaderReads:
    """Reads verify the header on what came back."""

    @pytest.mark.asyncio
    async def test_a_stale_row_yields_the_next_candidate(self):
        from apple_mail_mcp.server import _get_email_by_header

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = [
            ("uuid-work", "INBOX", 1),
            ("uuid-work", "Archive", 2),
        ]
        fetched = []

        async def by_id(mid, acct=None, mbox=None):
            fetched.append(mid)
            # ROWID 1 is somebody else's message now.
            return {
                "id": mid,
                "message_id": "<other@x>" if mid == 1 else "<a@x>",
            }

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            out = await _get_email_by_header("<a@x>", None, None)

        assert fetched == [1, 2]
        assert out["message_id"] == "<a@x>"

    @pytest.mark.asyncio
    async def test_a_stranger_is_never_returned(self):
        from apple_mail_mcp.server import _get_email_by_header

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 1)]

        async def by_id(mid, acct=None, mbox=None):
            return {"id": mid, "message_id": "<somebody-else@x>"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            with pytest.raises(ValueError, match="stale|not where"):
                await _get_email_by_header("<a@x>", None, None)

    @pytest.mark.asyncio
    async def test_an_unindexed_header_is_looked_for_live(self):
        """Not indexed yet is the normal state for anything that arrived
        after the last sync."""
        from apple_mail_mcp.server import _get_email_by_header

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []

        async def by_id(mid, acct=None, mbox=None):
            return {"id": mid, "message_id": "<fresh@x>"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._locate_header_via_jxa",
                AsyncMock(return_value=("Work", "INBOX", 99)),
            ),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            out = await _get_email_by_header("<fresh@x>", None, None)

        assert out["id"] == 99

    @pytest.mark.asyncio
    async def test_an_incomplete_live_search_is_not_a_verdict(self):
        from apple_mail_mcp.server import (
            _LiveLookupIncomplete,
            _get_email_by_header,
        )

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_by_rfc822.return_value = []

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._locate_header_via_jxa",
                AsyncMock(
                    side_effect=_LiveLookupIncomplete("3 mailbox(es) skipped")
                ),
            ),
        ):
            with pytest.raises(ValueError) as err:
                await _get_email_by_header("<x@x>", None, None)

        assert "could not be completed" in str(err.value)
        assert "deleted" not in str(err.value).lower()


class TestAStaleIndexIsNotTheEndOfTheSearch:
    """Every indexed location being stale says the INDEX is out of date,
    not that the message is gone. The read path used to stop there,
    which contradicts the rule this unit states: the index orders the
    search, it never limits it."""

    @pytest.mark.asyncio
    async def test_a_live_search_runs_after_every_row_proves_stale(self):
        from apple_mail_mcp.server import _get_email_by_header

        mgr = MagicMock()
        mgr.has_index.return_value = True
        # The index points at a ROWID that now holds a different message.
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 1)]

        async def by_id(mid, acct=None, mbox=None):
            return {
                "id": mid,
                "message_id": "<somebody-else@x>" if mid == 1 else "<a@x>",
            }

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch(
                "apple_mail_mcp.server._locate_header_via_jxa",
                AsyncMock(return_value=("Private", "Archive", 99)),
            ),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            out = await _get_email_by_header("<a@x>", None, None)

        assert out["id"] == 99, (
            "the message had moved to another account; the stale row "
            "must not end the search"
        )


class TestBracketsAreNotPartOfDeduplication:
    """`["<a@b>", "a@b"]` is one message twice. Collapsing on the raw
    string writes it twice and reports it as both updated and unchanged
    — against the bracket-insensitive identity this unit declares."""

    def test_the_two_spellings_collapse(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids(["<a@b>", "a@b"]) == ["<a@b>"]
        assert _normalize_message_ids(["a@b", "<a@b>"]) == ["a@b"]

    def test_different_messages_still_survive(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert len(_normalize_message_ids(["<a@b>", "<c@d>"])) == 2
