"""Tests for the write tools: set_flag and set_read_status.

Read paths, index internals and the JXA core moved to the
modules named after the code they exercise; what remains is
the write surface.

Like the rest of the suite, JXA execution is mocked so these
run without macOS / Mail.app.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._mocks import mock_acct_map as _mock_acct_map
from tests._mocks import mock_index as _mock_index


class TestWriteBuilder:
    """JXA script generation for batch property writes."""

    def _groups(self):
        return [{"account": "Work", "mailbox": "INBOX", "ids": [1, 2]}]

    def test_set_read_true(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(self._groups(), True).build()
        assert "msg.readStatus = true;" in script

    def test_set_read_false(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(self._groups(), False).build()
        assert "msg.readStatus = false;" in script

    def test_set_flag_with_color_sets_index_and_status(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            self._groups(), flagged=True, flag_index=3
        ).build()
        assert "msg.flaggedStatus = true;" in script
        assert "msg.flagIndex = 3;" in script

    def test_set_flag_default_no_forced_color(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            self._groups(), flagged=True, flag_index=None
        ).build()
        assert "msg.flaggedStatus = true;" in script
        assert "flagIndex" not in script

    def test_unflag_clears_status(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(self._groups(), flagged=False).build()
        assert "msg.flaggedStatus = false;" in script
        assert "flagIndex" not in script

    def test_groups_serialized_as_json(self):
        from apple_mail_mcp.builders import WriteBuilder

        groups = [
            {"account": "A", "mailbox": "INBOX", "ids": [10]},
            {"account": "B", "mailbox": "Archive", "ids": [20, 30]},
        ]
        script = WriteBuilder.set_read(groups, True).build()
        # The groups literal is embedded verbatim via json.dumps.
        assert json.dumps(groups) in script

    def test_returns_updated_and_not_found_shape(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(self._groups(), True).build()
        for bucket in (
            "updated: updated",
            "unchanged: unchanged",
            "not_found: notFound",
        ):
            assert bucket in script

    def test_account_name_with_quotes_is_escaped(self):
        """A pathological account name can't break out of the JS literal."""
        from apple_mail_mcp.builders import WriteBuilder

        groups = [
            {"account": 'ev"il', "mailbox": "INBOX", "ids": [1]},
        ]
        script = WriteBuilder.set_read(groups, True).build()
        # json.dumps escapes the embedded quote; the raw sequence must
        # not appear unescaped.
        assert '\\"il' in script

    def test_scan_group_generates_bounded_scan_loop(self):
        from apple_mail_mcp.builders import WriteBuilder

        groups = [{"account": "Work", "ids": [1, 2], "scan": True}]
        script = WriteBuilder.set_read(
            groups, True, max_scan_mailboxes=25
        ).build()
        assert "if (g.scan)" in script
        assert "account.mailboxes()" in script
        assert "MAX_SCAN = 25" in script
        assert "msg.readStatus = true;" in script

    def test_located_and_scan_groups_coexist(self):
        from apple_mail_mcp.builders import WriteBuilder

        groups = [
            {"account": "Work", "mailbox": "INBOX", "ids": [3]},
            {"account": "Work", "ids": [1, 2], "scan": True},
        ]
        script = WriteBuilder.set_flag(
            groups, flagged=True, flag_index=0
        ).build()
        # Both the located branch (getMailbox) and the scan branch are present.
        assert "MailCore.getMailbox(account, g.mailbox)" in script
        assert "if (g.scan)" in script


class TestNormalizeMessageIds:
    def test_single_int_becomes_list(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids(5) == [5]

    def test_list_passthrough(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids([1, 2, 3]) == [1, 2, 3]

    def test_dedup_preserves_order(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids([3, 1, 3, 2, 1]) == [3, 1, 2]

    def test_bool_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="must not be a bool"):
            _normalize_message_ids(True)

    def test_bool_in_list_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="must be an int"):
            _normalize_message_ids([1, True, 3])

    def test_message_id_header_accepted(self):
        """A Message-ID header is a first-class reference."""
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("<a@b.com>") == ["<a@b.com>"]
        assert _normalize_message_ids([1, " <a@b.com> ", 1, "<a@b.com>"]) == [
            1,
            "<a@b.com>",
        ]

    def test_blank_header_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="empty string"):
            _normalize_message_ids(["   "])

    def test_unsupported_type_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="must be an int id"):
            _normalize_message_ids([1, 2.5])

    def test_empty_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="empty"):
            _normalize_message_ids([])

    def test_oversize_batch_rejected(self):
        from apple_mail_mcp.server import (
            MAX_WRITE_BATCH,
            _normalize_message_ids,
        )

        ids = list(range(MAX_WRITE_BATCH + 1))
        with pytest.raises(ValueError, match="Too many ids"):
            _normalize_message_ids(ids)

    def test_max_batch_exactly_allowed(self):
        from apple_mail_mcp.server import (
            MAX_WRITE_BATCH,
            _normalize_message_ids,
        )

        ids = list(range(MAX_WRITE_BATCH))
        assert len(_normalize_message_ids(ids)) == MAX_WRITE_BATCH


class TestFlagColorMapping:
    def test_all_seven_colors_present(self):
        from apple_mail_mcp.builders import FLAG_COLOR_INDEX

        assert FLAG_COLOR_INDEX == {
            "red": 0,
            "orange": 1,
            "yellow": 2,
            "green": 3,
            "blue": 4,
            "purple": 5,
            "gray": 6,
        }


# ========== Shared mock helpers ==========


def _mock_index(location=None, has_index=True):
    """An IndexManager mock that resolves every id to `location`."""
    mgr = MagicMock()
    mgr.has_index.return_value = has_index
    mgr.find_email_location.return_value = location
    # One location per id unless a test says otherwise: a MagicMock here
    # makes the ambiguity guard raise on `> 1`.
    mgr.count_email_locations.return_value = 1 if location else 0
    return mgr


def _mock_acct_map(uuid_to_name="Work", excluded_uuids=None):
    m = MagicMock()
    m.ensure_loaded = AsyncMock()
    m.names_to_uuids.return_value = set(excluded_uuids or [])
    m.name_to_uuid.return_value = None
    m.uuid_to_name.return_value = uuid_to_name
    return m


class TestSetReadStatus:
    @pytest.mark.asyncio
    async def test_marks_read_via_index(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [42], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(42)

        assert result["updated"] == [42]
        assert result["not_found"] == []
        assert result["skipped_hidden"] == []
        assert "msg.readStatus = true;" in captured["script"]

    @pytest.mark.asyncio
    async def test_marks_unread(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [7], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            await set_read_status([7], read=False)

        assert "msg.readStatus = false;" in captured["script"]


class TestSetFlag:
    @pytest.mark.asyncio
    async def test_flag_red_sets_index_zero(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [1], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag(1, color="red")

        assert "msg.flagIndex = 0;" in captured["script"]

    @pytest.mark.asyncio
    async def test_flag_purple_sets_index_five(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [1], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag(1, color="purple")

        assert "msg.flagIndex = 5;" in captured["script"]

    @pytest.mark.asyncio
    async def test_unflag_with_none(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [1], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag(1, color="none")

        assert "msg.flaggedStatus = false;" in captured["script"]

    @pytest.mark.asyncio
    async def test_default_flag_no_color(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [1], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag(1)  # color defaults to "default"

        assert "msg.flaggedStatus = true;" in captured["script"]
        assert "flagIndex" not in captured["script"]


class TestWriteBuckets:
    @pytest.mark.asyncio
    async def test_not_found_merges_python_and_jxa(self):
        """Unresolvable ids (Python) + absent-in-mailbox ids (JXA) merge."""

        # id 99 has no index location and no hint → Python not_found.
        # id 1 resolves but JXA reports it absent → JXA not_found.
        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX") if mid == 1 else None

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = locate
        amap = _mock_acct_map()

        # id 99 has no index location, so it goes to the bounded scan;
        # neither is found by JXA.
        async def fake_exec(script, **kw):
            if '"scan": true' in script:
                return {"updated": [], "not_found": [99]}
            return {"updated": [], "not_found": [1]}

        mgr.get_rfc822_id.return_value = None  # nothing to recover with

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1, 99])

        assert set(result["not_found"]) == {1, 99}
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_unresolvable_ids_go_to_a_bounded_scan(self):
        """Ids the index can't place are scanned for, not written off —
        even with no account given (JXA then uses the first account)."""
        mgr = _mock_index(location=None)  # index never resolves them
        mgr.get_rfc822_id.return_value = None
        amap = _mock_acct_map()
        scripts = []

        async def fake_exec(script, **kw):
            scripts.append(script)
            return {"updated": [], "not_found": [1, 2]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1, 2])

        assert len(scripts) == 1
        assert '"scan": true' in scripts[0]
        assert set(result["not_found"]) == {1, 2}

    @pytest.mark.asyncio
    async def test_hint_when_no_index(self):
        mgr = _mock_index(location=None, has_index=False)
        amap = _mock_acct_map()

        async def fake_exec(script, **kw):
            return {"updated": [], "unchanged": [], "not_found": [1]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1])

        assert "hint" in result
        assert "index" in result["hint"].lower()

    @pytest.mark.asyncio
    async def test_hint_path_works_without_index(self):
        """With no index but account+mailbox hints, ids still dispatch."""
        mgr = _mock_index(location=None, has_index=False)
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [5], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(5, account="Work", mailbox="INBOX")

        assert result["updated"] == [5]
        assert '"account": "Work"' in captured["script"]


class TestExcludedAccountGate:
    @pytest.mark.asyncio
    async def test_explicit_hidden_account_skips_all(self, monkeypatch):
        monkeypatch.setenv("APPLE_MAIL_INDEX_EXCLUDE_ACCOUNTS", "Secret")
        exec_mock = AsyncMock()
        mgr = _mock_index(location=("uuid-x", "INBOX"))
        amap = _mock_acct_map()

        with (
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag([1, 2], account="Secret", color="red")

        exec_mock.assert_not_called()
        assert set(result["skipped_hidden"]) == {1, 2}
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_id_resolving_into_excluded_account_skipped(
        self, monkeypatch
    ):
        """An id whose index location is a hidden account never hits JXA."""
        monkeypatch.setenv("APPLE_MAIL_INDEX_EXCLUDE_ACCOUNTS", "Secret")
        mgr = _mock_index(location=("uuid-secret", "INBOX"))
        amap = _mock_acct_map(excluded_uuids={"uuid-secret"})
        exec_mock = AsyncMock()

        with (
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1])

        exec_mock.assert_not_called()
        assert result["skipped_hidden"] == [1]


class TestScanFallback:
    """Index-free / index-miss write resolution via a bounded JXA scan."""

    @pytest.mark.asyncio
    async def test_scan_group_when_index_misses_and_account_given(self):
        """An id the index can't place scans the given account's mailboxes."""
        mgr = _mock_index(location=None)  # index present, but misses the id
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [9], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(9, color="red", account="Work")

        assert result["updated"] == [9]
        assert '"scan": true' in captured["script"]
        assert "msg.flagIndex = 0;" in captured["script"]

    @pytest.mark.asyncio
    async def test_scan_group_when_no_index(self):
        mgr = _mock_index(location=None, has_index=False)
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [5, 6], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([5, 6], account="Work")

        assert set(result["updated"]) == {5, 6}
        assert '"scan": true' in captured["script"]

    @pytest.mark.asyncio
    async def test_scan_timeout_reports_not_found(self):
        """A timed-out scan reports its ids as not_found, not an error."""
        mgr = _mock_index(location=None)
        amap = _mock_acct_map()

        async def boom(script, **kw):
            raise TimeoutError("scan too slow")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=boom,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(9, account="Work")

        # A timed-out scan is Mail being unreachable, not a verdict on
        # the message: it belongs in `failed`, never in `not_found`.
        assert result["failed"] == [9]
        assert result["not_found"] == []
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_located_write_survives_scan_timeout(self):
        """A located write commits even if a sibling scan times out."""

        # id 1 has an index location; id 2 does not → scan (which fails).
        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX") if mid == 1 else None

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = locate
        amap = _mock_acct_map()

        async def exec_router(script, **kw):
            if '"scan": true' in script:
                raise TimeoutError("scan too slow")
            return {"updated": [1], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=exec_router,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1, 2], account="Work")

        assert result["updated"] == [1]  # located write survived
        assert result["failed"] == [2]  # the scan never reached Mail


class TestReadOnlyRefusal:
    @pytest.mark.asyncio
    async def test_set_flag_refused_read_only(self):
        exec_mock = AsyncMock()
        with (
            patch(
                "apple_mail_mcp.server.get_read_only_mode", return_value=True
            ),
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
        ):
            from apple_mail_mcp.server import set_flag

            with pytest.raises(PermissionError):
                await set_flag(1, color="red")

        exec_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_read_status_refused_read_only(self):
        exec_mock = AsyncMock()
        with (
            patch(
                "apple_mail_mcp.server.get_read_only_mode", return_value=True
            ),
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
        ):
            from apple_mail_mcp.server import set_read_status

            with pytest.raises(PermissionError):
                await set_read_status(1)

        exec_mock.assert_not_called()


class TestWriteTargetsVerifiedById:
    """A write must never land on a different message than requested."""

    def test_identity_is_checked_before_applying(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}],
            flagged=True,
            flag_index=0,
        ).build()
        # The id snapshot can go stale between fetch and write, so the
        # generated script must verify identity and re-resolve by id.
        assert "function applyToMessage" in script
        assert "msg.id() !== targetId" in script
        assert "collection.byId(targetId)" in script

    def test_no_unguarded_positional_write_remains(self):
        from apple_mail_mcp.builders import WriteBuilder

        for groups in (
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}],
            [{"account": "W", "ids": [1], "scan": True}],
        ):
            script = WriteBuilder.set_read(groups, True).build()
            assert "const msg = mailbox.messages[idx]" not in script
            assert "const msg = mailboxes[m].messages[idx]" not in script
            assert "applyToMessage(" in script


class TestNoOpWritesAreSkipped:
    """Don't re-write state that already matches — each write is a
    server round-trip for IMAP/Exchange and rotates the Exchange ItemId."""

    def test_flag_color_checks_live_state(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}],
            flagged=True,
            flag_index=4,
        ).build()
        assert "msg.flaggedStatus() !== true" in script
        assert "msg.flagIndex() !== 4" in script
        assert 'return "unchanged"' in script

    def test_read_checks_live_state(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}], False
        ).build()
        assert "msg.readStatus() !== false" in script

    def test_unflag_checks_live_state(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}], flagged=False
        ).build()
        assert "msg.flaggedStatus() !== false" in script

    def test_state_is_read_from_mail_not_an_index(self):
        """The check must be live JXA — a stale index caused the original
        'it's already flagged' bug."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "mailbox": "INBOX", "ids": [1]}],
            flagged=True,
            flag_index=0,
        ).build()
        # Predicate calls the live accessor on the resolved message.
        assert "if (!(msg.flaggedStatus()" in script

    @pytest.mark.asyncio
    async def test_unchanged_ids_are_surfaced(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()

        async def fake_exec(script, **kw):
            return {"updated": [1], "unchanged": [2], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            r = await set_flag([1, 2], color="red")

        assert r["updated"] == [1]
        assert r["unchanged"] == [2]
        assert r["not_found"] == []


class TestMovedMessageRecovery:
    """A write must survive another device moving the message."""

    @pytest.mark.asyncio
    async def test_recovers_via_header_and_reports_move(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        # id 5 resolves in the index, but JXA no longer finds it there.
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")
        mgr.get_rfc822_id.return_value = "<moved@x>"
        amap = _mock_acct_map()

        calls = []

        async def fake_exec(script, **kw):
            calls.append(script)
            if '"by_header": true' in script:  # the group data, not the JS
                return {
                    "updated": ["<moved@x>"],
                    "unchanged": [],
                    "not_found": [],
                }
            return {"updated": [], "unchanged": [], "not_found": [5]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            r = await set_flag(5, color="red")

        assert r["updated"] == [5]  # mapped back to the caller's id
        assert r["not_found"] == []
        assert "moved" in r["hint"].lower()
        assert len(calls) == 2  # normal write, then recovery

    @pytest.mark.asyncio
    async def test_no_stable_id_means_no_retry(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")
        mgr.get_rfc822_id.return_value = None  # indexed before v6
        amap = _mock_acct_map()

        calls = []

        async def fake_exec(script, **kw):
            calls.append(script)
            return {"updated": [], "unchanged": [], "not_found": [5]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            r = await set_flag(5, color="red")

        assert r["not_found"] == [5]
        assert len(calls) == 1  # no pointless second scan
        assert "refresh_index" in r["hint"]


class TestReviewFindings:
    """Regressions for the defects the code review surfaced."""

    # --- data corruption in the write path -------------------------

    def test_header_catch_handles_both_group_shapes(self):
        """A catch reading g.ids on a header group throws inside the
        catch, which escapes and kills the entire batch."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "W", "headers": ["<a@b>"], "by_header": True}], True
        ).build()
        assert "g.ids || g.headers" in script
        assert (
            "for (const id of g.ids) notFound.push(id);\n    continue"
            not in (script)
        )

    def test_recovery_never_writes_into_trash_or_junk(self):
        """Still true — but decided by role, so it survives a locale.

        The script used to carry its own lowercase word list, which
        held only for English installs. See
        TestWellKnownMailboxesResolveByRole for the matrix.
        """
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "headers": ["<a@b>"], "by_header": True}],
            flagged=True,
        ).build()
        assert "MailCore.isDiscardMailbox(nm)" in script
        assert "isDiscard && !isPreferred" in script

    def test_header_is_retired_only_after_a_successful_apply(self):
        """Retiring on match meant a failed apply consumed the header
        and no later mailbox was tried."""
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "W", "headers": ["<a@b>"], "by_header": True}], True
        ).build()
        applied = script.index("r = applyByHeader")
        retired = script.index("remaining.delete(target)")
        assert applied < retired

    @pytest.mark.asyncio
    async def test_recovery_lookup_is_scoped_to_the_resolved_mailbox(self):
        """Unscoped, get_rfc822_id can return another message's header
        (ids are unique per mailbox only) — and we'd flag that message."""
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")
        mgr.get_rfc822_id.return_value = "<moved@x>"
        amap = _mock_acct_map()

        async def fake_exec(script, **kw):
            if '"by_header": true' in script:
                return {
                    "updated": ["<moved@x>"],
                    "unchanged": [],
                    "not_found": [],
                }
            return {"updated": [], "unchanged": [], "not_found": [5]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag(5, color="red")

        # Scope must be passed, not omitted.
        args = mgr.get_rfc822_id.call_args[0]
        assert args[0] == 5
        assert args[1] == "uuid-work"
        assert args[2] == "INBOX"

    @pytest.mark.asyncio
    async def test_ambiguous_header_is_not_credited_to_every_id(self):
        """Two ids sharing one header: JXA writes ONE copy, so crediting
        both would report a write that never happened."""

        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX" if mid == 1 else "Archive")

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        mgr.find_email_location.side_effect = locate
        mgr.get_rfc822_id.return_value = "<dup@x>"  # same header for both
        amap = _mock_acct_map()
        calls = []

        async def fake_exec(script, **kw):
            calls.append(script)
            return {"updated": [], "unchanged": [], "not_found": [1, 2]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            r = await set_flag([1, 2], color="red")

        assert set(r["not_found"]) == {1, 2}
        assert r.get("failed", []) == []  # Mail answered; it just missed
        assert r["updated"] == []
        # Ambiguous -> no recovery attempt at all.
        assert not any('"by_header": true' in s for s in calls)

    @pytest.mark.asyncio
    async def test_located_write_failure_still_buckets_every_id(self):
        """The contract is exactly-once bucketing; a raising osascript
        must not leave ids in no bucket at all."""
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        mgr.get_rfc822_id.return_value = None
        amap = _mock_acct_map()

        async def boom(script, **kw):
            raise TimeoutError("osascript wedged")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=boom,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            r = await set_read_status([1, 2])

        # Exactly-once bucketing still holds — but a wedged osascript is
        # a failure, not a missing message.
        assert set(r["failed"]) == {1, 2}
        assert r["not_found"] == []

    def test_located_write_has_a_bounded_timeout(self):
        from apple_mail_mcp.server import WRITE_TIMEOUT

        assert 5 <= WRITE_TIMEOUT <= 300

    def test_recovery_gets_a_larger_budget_than_the_id_scan(self):
        """Header scans fetch strings, not ints — the 15s id-scan budget
        would make recovery time out exactly when it is needed."""
        from apple_mail_mcp.server import RECOVERY_TIMEOUT, STRATEGY3_TIMEOUT

        assert RECOVERY_TIMEOUT > STRATEGY3_TIMEOUT

    # --- config validation -----------------------------------------

    def test_zero_size_limit_is_rejected_in_toml(self, tmp_path, monkeypatch):
        import apple_mail_mcp.config as cfg

        f = tmp_path / "config.toml"
        f.write_text("config_version = 1\n[index]\nmax_email_mb = 0\n")
        monkeypatch.setattr(cfg, "CONFIG_FILE_PATH", f)
        cfg._invalidate_config_cache()
        try:
            with pytest.raises(cfg.ConfigError, match="must be > 0"):
                cfg._load_config_file()
        finally:
            cfg._invalidate_config_cache()

    def test_zero_size_limit_from_env_falls_back_to_default(self, monkeypatch):
        """0 would skip every message and flood the DLQ."""
        import apple_mail_mcp.config as cfg

        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "0")
        assert cfg.get_index_max_email_mb() == 25.0
        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "-5")
        assert cfg.get_index_max_email_mb() == 25.0
        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "nonsense")
        assert cfg.get_index_max_email_mb() == 25.0

    # --- skip accounting across all three paths --------------------

    def test_all_index_paths_record_oversized_skips(self):
        """The build recorded skips; sync and watcher dropped them."""
        from pathlib import Path as _P

        for mod in ("sync", "watcher"):
            src = _P(f"src/apple_mail_mcp/index/{mod}.py").read_text()
            assert "emlx_too_large" in src, mod
            assert "SKIP_REASON_TOO_LARGE" in src, mod

    def test_skip_row_matches_what_the_counter_queries(self):
        from apple_mail_mcp.index.schema import (
            SKIP_REASON_TOO_LARGE,
            skip_row,
        )

        row = skip_row("/p", "acct", "INBOX", SKIP_REASON_TOO_LARGE)
        # count_skipped_too_large matches error_message = 'too_large'
        assert row[4] == "too_large"


class TestMessageIdIsAcceptedForWrites:
    """Callers may address a message by its stable RFC822 header.

    The point of the header is that it does not depend on the index
    being current: a row may say the mail is in INBOX while a phone
    has long filed it in Archiv. So the header must never be
    translated back into a ROWID — JXA has to match on
    `messageId()` itself. These tests pin that down.
    """

    @pytest.mark.asyncio
    async def test_header_is_written_by_header_not_by_rowid(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = [("uuid-work", "Archiv", 4242)]
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": ["<a@x.com>"], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<a@x.com>", color="red")

        assert result["updated"] == ["<a@x.com>"]
        script = captured["script"]
        assert '"by_header": true' in script
        # The index only orders the scan; it never becomes the target.
        assert "4242" not in script
        assert "Archiv" in script  # prefer_mailboxes hint

    @pytest.mark.asyncio
    async def test_header_without_index_scans_visible_account(self):
        mgr = _mock_index(location=None, has_index=False)
        amap = _mock_acct_map()
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": ["<b@x.com>"], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status("<b@x.com>", account="Work")

        assert result["updated"] == ["<b@x.com>"]
        assert '"by_header": true' in captured["script"]

    @pytest.mark.asyncio
    async def test_header_in_excluded_account_is_skipped(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = [("uuid-secret", "INBOX", 1)]
        amap = _mock_acct_map(excluded_uuids={"uuid-secret"})
        calls = []

        async def fake_exec(script, **kw):
            calls.append(script)
            return {"updated": [], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<c@x.com>", color="red")

        assert result["skipped_hidden"] == ["<c@x.com>"]
        assert calls == []  # nothing ever reached Apple Mail

    @pytest.mark.asyncio
    async def test_mixed_batch_keeps_both_forms_apart(self):
        """Ints and headers run in separate calls and both come back."""

        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX")

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = locate
        mgr.find_by_rfc822.return_value = [("uuid-work", "Archiv", 77)]
        amap = _mock_acct_map()
        scripts = []

        async def exec_router(script, **kw):
            scripts.append(script)
            if '"by_header": true' in script:
                return {"updated": ["<d@x.com>"], "not_found": []}
            return {"updated": [7], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=exec_router,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([7, "<d@x.com>"])

        assert len(scripts) == 2  # located write and header write separated
        assert set(result["updated"]) == {7, "<d@x.com>"}
        assert result["not_found"] == []

    @pytest.mark.asyncio
    async def test_failed_header_write_is_reported_as_header(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()

        async def boom(script, **kw):
            raise TimeoutError("too slow")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=boom,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<e@x.com>", color="red")

        assert result["failed"] == ["<e@x.com>"]
        assert result["not_found"] == []
        # A header write is already keyed on the stable id — there is
        # nothing left for the ROWID recovery path to do.
        mgr.get_rfc822_id.assert_not_called()


class TestFailedWritesAreNotReportedAsNotFound:
    """A write that never reached Apple Mail is not a verdict on the mail.

    Reported live by a scheduled triage task: every set_flag call came
    back `not_found` with a hint blaming the index, while the messages
    were sitting in the mailbox untouched. Reads kept working because
    they are served from the index and the .emlx files — only writes
    need Apple Events, and those were refused.
    """

    @pytest.mark.asyncio
    async def test_osascript_failure_lands_in_failed_with_the_real_error(self):
        mgr = _mock_index(location=("uuid-work", "Junk"))
        amap = _mock_acct_map()

        async def refused(script, **kw):
            raise RuntimeError(
                "execution error: Not authorized to send Apple events "
                "to Mail. (-1743)"
            )

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=refused,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(12345, color="orange")

        assert result["failed"] == [12345]
        assert result["not_found"] == []  # never claim the mail is gone
        assert "-1743" in result["error"]
        assert "Automation" in result["hint"]

    @pytest.mark.asyncio
    async def test_genuine_miss_still_reports_not_found(self):
        """The distinction only helps if a real miss is still a miss."""
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()

        async def empty(script, **kw):
            return {"updated": [], "unchanged": [], "not_found": [999]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=empty,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(999)

        assert result["not_found"] == [999]
        assert result.get("failed", []) == []


class TestJunkIsAWritableLocation:
    """A message that lives in Junk is a legitimate target.

    The discard rule exists so a *recovered* write never lands on the
    Trash copy of a message that was re-filed. Applying it
    unconditionally made "flag this junk mail" impossible — exactly
    what the triage task tried first.
    """

    def _script(self, prefer):
        from apple_mail_mcp.builders import WriteBuilder

        return WriteBuilder(
            groups=[
                {
                    "account": "Work",
                    "headers": ["<a@x.com>"],
                    "prefer_mailboxes": prefer,
                    "by_header": True,
                }
            ],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()

    def test_discard_mailbox_is_scanned_when_the_index_names_it(self):
        js = self._script(["Junk"])
        assert "isDiscard && !isPreferred" in js
        assert '"prefer_mailboxes": ["Junk"]' in js

    def test_discard_is_decided_by_role_not_by_a_local_word_list(self):
        """Duplicating the names here would rot on the first locale."""
        js = self._script(["Archiv"])
        assert "MailCore.isDiscardMailbox(nm)" in js
        assert "DISCARD_MAILBOXES" not in js


class TestJxaFailuresCarryTheirReason:
    """The write script must say why, not just "not found".

    A scheduled task reported every set_flag call as not_found, and the
    server log was empty — because a failing account or mailbox lookup
    inside the JXA script pushed every target into notFound without
    raising anything. There was nothing left to diagnose from.
    """

    def test_script_reports_failures_with_a_reason(self):
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder(
            groups=[{"account": "Work", "mailbox": "Junk", "ids": [1]}],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()

        assert "failures: failures" in js
        assert "no such account: " in js
        assert "cannot open mailbox " in js
        assert "cannot list mailboxes of account " in js

    @pytest.mark.asyncio
    async def test_reason_reaches_the_caller_and_shapes_the_hint(self):
        mgr = _mock_index(location=("uuid-work", "Junk"))
        amap = _mock_acct_map()

        async def jxa(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": [],
                "failures": [
                    {
                        "target": 12345,
                        "reason": "no such account: uuid-work (Error)",
                    }
                ],
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(12345, color="orange")

        assert result["failed"] == [12345]
        assert result["not_found"] == []
        assert "no such account" in result["error"]
        # The hint must name THIS cause, not a generic permission story.
        assert "does not match any account" in result["hint"]

    @pytest.mark.asyncio
    async def test_mailbox_failure_suggests_the_message_id_route(self):
        mgr = _mock_index(location=("uuid-work", "Junk"))
        amap = _mock_acct_map()

        async def jxa(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": [],
                "failures": [
                    {"target": 7, "reason": "cannot open mailbox Junk (Error)"}
                ],
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(7)

        assert result["failed"] == [7]
        assert "Message-ID" in result["hint"]


class TestAngleBracketsDoNotBreakIdentity:
    """`<a@b>` and `a@b` are the same message.

    The .emlx header keeps the angle brackets, Apple Mail's messageId
    property drops them. Every comparison between the two was a strict
    string compare, so it could never match: two days of Message-ID
    writes and the whole move-recovery path reported a mute "not found"
    for messages that were sitting right there.
    """

    def test_stored_form_really_keeps_the_brackets(self):
        import email as email_mod

        from apple_mail_mcp.index.disk import header_text

        msg = email_mod.message_from_string(
            "Message-ID: <Oz9@geopod-ismtpd-11>\r\nSubject: x\r\n\r\nb"
        )
        assert header_text(msg, "Message-ID") == "<Oz9@geopod-ismtpd-11>"

    def test_header_key_collapses_both_forms(self):
        from apple_mail_mcp.server import _header_key

        assert _header_key("<a@b.com>") == _header_key("a@b.com")
        assert _header_key(" <a@b.com> ") == "a@b.com"
        assert _header_key(None) == ""
        # A bracket that is not a wrapper must survive untouched.
        assert _header_key("a<b@c.com") == "a<b@c.com"

    def test_jxa_compares_on_the_bare_form(self):
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder(
            groups=[
                {
                    "account": "Work",
                    "headers": ["<a@b.com>"],
                    "prefer_mailboxes": ["INBOX"],
                    "by_header": True,
                }
            ],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()

        assert "function normHeader(" in js
        assert "normed.indexOf(normHeader(target))" in js
        assert "normHeader(msg.messageId()) !== normHeader(targetHeader)" in js
        # Raw comparisons must be gone.
        assert "headers.indexOf(target)" not in js

    def test_index_lookup_matches_either_form(self):
        """Brackets, and any stray whitespace a folded header brings.

        A strict comparison here fails silently and the caller then
        searches the wrong account — the failure mode that cost a full
        debugging session.
        """
        import sqlite3
        import tempfile

        from apple_mail_mcp.index.manager import IndexManager

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE emails (account TEXT, mailbox TEXT, "
            "message_id INT, rfc822_message_id TEXT, indexed_at TEXT)"
        )
        stored_forms = [
            "<a@x.com>",  # as the .emlx header carries it
            "b@x.com",  # as Apple Mail reports it
            " <c@x.com>\n",  # folded / padded
            "\t<d@x.com>\r\n",
        ]
        for i, stored in enumerate(stored_forms):
            conn.execute(
                "INSERT INTO emails VALUES ('U','Posteingang',?,?,'2026')",
                (i, stored),
            )
        conn.commit()

        mgr = IndexManager.__new__(IndexManager)
        mgr._get_conn = lambda: conn  # type: ignore[assignment]
        try:
            for i, stored in enumerate(stored_forms):
                bare = stored.strip().strip("<>").strip()
                for probe in (bare, f"<{bare}>"):
                    found = mgr.find_by_rfc822(probe)
                    assert found, f"{probe!r} missed stored {stored!r}"
                    assert found[0][2] == i
        finally:
            conn.close()
            os.unlink(path)


class TestUnindexedHeaderSearchesEveryAccount:
    """A message that arrived after the last sync must still be writable.

    Measured live: in one mailbox the OLDEST message (2013) flagged
    fine while the NEWEST (27 minutes after the last sync) came back
    not_found. The index could not place it, and the code then searched
    exactly one account — the default. Anything living elsewhere was
    unreachable, and said so as a mute "not found".
    """

    @pytest.mark.asyncio
    async def test_all_visible_accounts_are_searched(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []  # not indexed yet
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [
            {"name": "byte5"},
            {"name": "Privat"},
            {"name": "Verein"},
        ]
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": ["<new@x.com>"], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<new@x.com>", color="green")

        assert result["updated"] == ["<new@x.com>"]
        for name in ("byte5", "Privat", "Verein"):
            assert f'"account": "{name}"' in captured["script"]

    @pytest.mark.asyncio
    async def test_explicit_account_is_still_honoured(self):
        """Pinning an account must not fan out to the others."""
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [
            {"name": "byte5"},
            {"name": "Privat"},
        ]
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": ["<a@x.com>"], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag("<a@x.com>", color="green", account="byte5")

        assert '"account": "byte5"' in captured["script"]
        assert '"Privat"' not in captured["script"]

    def test_a_header_is_written_once_across_accounts(self):
        """Fanning out must not flag two copies or invent a miss."""
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder(
            groups=[
                {"account": "A", "headers": ["<x@y>"], "by_header": True},
                {"account": "B", "headers": ["<x@y>"], "by_header": True},
            ],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()

        assert "const settled = new Set();" in js
        assert "settled.add(String(target));" in js
        # A miss in one account must not survive a hit in another.
        assert "notFound.filter((t) => !settled.has(String(t)))" in js


class TestIndexOrdersTheSearchButNeverLimitsIt:
    """A stale index row must not end the search in silence.

    Measured: the index placed a message in account byte5 / Posteingang
    and Apple Mail agreed it was there — yet set_flag reported
    not_found. Whatever the index says, it is at best where the message
    WAS. It now decides the ORDER of the search; the other visible
    accounts follow, and cost nothing once the header has been settled.
    """

    @pytest.mark.asyncio
    async def test_indexed_account_comes_first_others_follow(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = [("uuid-byte5", "Posteingang", 1)]
        amap = _mock_acct_map(uuid_to_name="byte5")
        amap.get_cached_accounts.return_value = [
            {"name": "iCloud"},
            {"name": "byte5"},
            {"name": "freenea"},
        ]
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": ["<a@x.com>"], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            await set_flag("<a@x.com>", color="green")

        script = captured["script"]
        order = [
            script.index(f'"account": "{n}"')
            for n in ("byte5", "iCloud", "freenea")
        ]
        assert order[0] < order[1] < order[2]  # index hint leads
        assert '"prefer_mailboxes": ["Posteingang"]' in script

    def test_a_settled_header_skips_the_remaining_accounts(self):
        """Broader search must stay free when the first account hits."""
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder(
            groups=[
                {"account": "A", "headers": ["<x@y>"], "by_header": True},
                {"account": "B", "headers": ["<x@y>"], "by_header": True},
            ],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()
        assert "if (remaining.size === 0) continue;" in js


class TestJsonEncodedListParameter:
    """`'["<a@b>"]'` is a list the client stringified, not a Message-ID.

    Observed live on 0.13.1: set_flag returned not_found for a message
    Apple Mail demonstrably had, and the echoed reference was the whole
    JSON array as one string. Widening the parameter from int to str is
    what made this possible — an int parameter rejected such input
    loudly, while a str parameter accepts any nonsense as an identifier
    and then reports a mute miss for a message sitting right there.
    """

    def test_stringified_list_is_unwrapped(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids('["<a@b.com>"]') == ["<a@b.com>"]
        assert _normalize_message_ids('["<a@b>", "<c@d>"]') == [
            "<a@b>",
            "<c@d>",
        ]
        assert _normalize_message_ids("[123, 456]") == [123, 456]

    def test_stray_quotes_are_stripped(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids('"<a@b.com>"') == ["<a@b.com>"]

    def test_empty_stringified_list_is_refused(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="empty"):
            _normalize_message_ids("[]")

    @pytest.mark.parametrize(
        "bad", ["<a b@c>", 'has "quotes"', "line\nbreak", "tab\there"]
    )
    def test_malformed_reference_is_refused_loudly(self, bad):
        """Better a clear error than a silent hunt for a ghost."""
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="not a usable message"):
            _normalize_message_ids(bad)

    @pytest.mark.asyncio
    async def test_the_reported_case_now_reaches_apple_mail(self):
        header = "<327950738.3015094.1785178460649@allianz.de>"
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = [("uuid-byte5", "Posteingang", 1)]
        amap = _mock_acct_map(uuid_to_name="byte5")
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]
        captured = {}

        async def fake_exec(script, **kw):
            captured["script"] = script
            return {"updated": [header], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=fake_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(f'["{header}"]', color="green")

        assert result["updated"] == [header]
        assert '\\"' not in captured["script"]  # no JSON text as an id


class TestAFailedWriteExplainsItself:
    """A bare not_found is unfalsifiable from the outside.

    Three separate causes produced the identical empty answer in one
    day: a stringified list taken for a Message-ID, a stale index row
    pinning the search to one account, and a genuinely deleted message.
    The result now states what was attempted, so the next occurrence is
    one call instead of a session.
    """

    @pytest.mark.asyncio
    async def test_not_found_reports_what_was_searched(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = [("uuid-byte5", "Posteingang", 1)]
        amap = _mock_acct_map(uuid_to_name="byte5")
        amap.get_cached_accounts.return_value = [
            {"name": "byte5"},
            {"name": "iCloud"},
        ]

        async def miss(script, **kw):
            return {"updated": [], "unchanged": [], "not_found": ["<a@x>"]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=miss,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<a@x>", color="green")

        diag = result["diagnostics"]
        assert diag["accounts_searched"] == ["byte5", "iCloud"]
        assert diag["mailboxes_preferred"] == ["Posteingang"]
        assert diag["references_as_received"] == ["<a@x>"]

    @pytest.mark.asyncio
    async def test_a_clean_success_stays_quiet(self):
        """Diagnostics are for failures; a success must not carry noise."""
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()

        async def hit(script, **kw):
            return {"updated": [7], "unchanged": [], "not_found": []}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=hit,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(7)

        assert "diagnostics" not in result


class TestHtmlEscapedReferences:
    """`&lt;a@b&gt;` is the same message as `<a@b>`.

    Observed live: the caller passed HTML-escaped brackets and had to
    notice and retry by itself. Nothing in an escaped reference trips
    the malformed-input checks, so it would otherwise sail through and
    miss in silence — the third input shape in one day to produce a
    mute not_found.
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("&lt;a@b.com&gt;", ["<a@b.com>"]),
            ("[&quot;&lt;a@b&gt;&quot;]", ["<a@b>"]),
            (
                "[&quot;&lt;a@b&gt;&quot;, &quot;&lt;c@d&gt;&quot;]",
                ["<a@b>", "<c@d>"],
            ),
            # `&` is legal in a Message-ID, so "&amp;" must survive
            # untouched: decoding it would name a DIFFERENT message and
            # a write would land on that one while reporting success.
            ("a&amp;b@x.com", ["a&amp;b@x.com"]),
        ],
    )
    def test_escaped_forms_are_repaired(self, given, expected):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids(given) == expected

    def test_unescaping_happens_before_unwrapping(self):
        """An escaped list must become a list before it is unwrapped."""
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("[&quot;&lt;x@y&gt;&quot;]") == ["<x@y>"]


class TestAmpersandIsNotStructure:
    """`&` is legal in a Message-ID local part.

    "<a&amp;b@x>" and "<a&b@x>" can both exist as distinct messages.
    Decoding the entity would aim a write at the other one and report
    success — unrecoverable. A reference that fails to match instead is
    answered with a miss, which the caller can act on.
    """

    def test_ampersand_entity_survives_untouched(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("<a&amp;b@x.com>") == ["<a&amp;b@x.com>"]
        assert _normalize_message_ids("a&amp;b@x.com") == ["a&amp;b@x.com"]

    def test_structural_entities_are_still_decoded(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("&lt;a@b&gt;") == ["<a@b>"]
        assert _normalize_message_ids("[&quot;&lt;a@b&gt;&quot;]") == ["<a@b>"]


class TestWriteScanReportsWhatItSkipped:
    """The write path had the read path's defect too.

    A header scan is capped at STRATEGY3_MAX_MAILBOXES and skips
    mailboxes Mail refuses. A miss that follows either of those is not
    evidence the message is gone, and the hint must not say it is.
    """

    def test_the_script_counts_what_it_left_out(self):
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder(
            groups=[{"account": "A", "headers": ["<x@y>"], "by_header": True}],
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()
        assert "cappedBoxes" in js
        assert "unreadableBoxes++" in js
        assert "scan_capped: cappedBoxes" in js

    @pytest.mark.asyncio
    async def test_a_capped_scan_does_not_claim_deletion(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def capped(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": ["<x@y>"],
                "scan_capped": 7,
                "scan_unreadable": 2,
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=capped,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<x@y>", color="red")

        assert result["not_found"] == ["<x@y>"]
        assert "9 mailbox(es) were never searched" in result["hint"]
        assert "deleted" not in result["hint"]
        assert result["diagnostics"]["mailboxes_not_searched"] == 9

    @pytest.mark.asyncio
    async def test_a_complete_scan_may_still_conclude_deletion(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def complete(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": ["<x@y>"],
                "scan_capped": 0,
                "scan_unreadable": 0,
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=complete,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<x@y>", color="red")

        assert "every mailbox was searched" in result["hint"]


class TestEveryScanPathCountsWhatItSkipped:
    """Second review round: two scan paths still claimed full coverage.

    The numeric-id scan never counted its own cap or unreadable
    mailboxes, and a trash/junk mailbox that is deliberately skipped was
    counted as searched — so a message living only there could be
    declared deleted.
    """

    def _js(self, groups):
        from apple_mail_mcp.builders import WriteBuilder

        return WriteBuilder(
            groups=groups,
            apply_js="msg.flaggedStatus = true;",
            needs_change_js="msg.flaggedStatus() === true",
        ).build()

    def test_numeric_scan_counts_its_cap_and_unreadable(self):
        js = self._js([{"account": "A", "ids": [1], "scan": True}])
        assert "cappedBoxes += Math.max(0, mailboxes.length - limit);" in js
        # the id() failure path must count, not silently continue
        assert js.count("unreadableBoxes++") >= 2

    def test_a_skipped_discard_mailbox_is_reported(self):
        js = self._js(
            [{"account": "A", "headers": ["<x@y>"], "by_header": True}]
        )
        assert "skippedDiscard++" in js
        assert "scan_skipped_discard: skippedDiscard" in js

    @pytest.mark.asyncio
    async def test_a_skipped_trash_mailbox_prevents_a_deletion_verdict(self):
        mgr = _mock_index(location=None)
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def skipped_trash(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": ["<x@y>"],
                "scan_capped": 0,
                "scan_unreadable": 0,
                "scan_skipped_discard": 2,
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=skipped_trash,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag("<x@y>", color="red")

        assert "2 mailbox(es) were never searched" in result["hint"]
        assert "trash/junk" in result["hint"]
        assert "deleted" not in result["hint"]
        assert result["diagnostics"]["mailboxes_not_searched"] == 2


class TestTheLastTwoGapPaths:
    """Fourth review round. Both were half-fixes of my own.

    The recovery gap was counted but never read in the numeric branch,
    and an account whose mailbox list cannot be enumerated at all threw
    before the per-mailbox catch, so no INCOMPLETE marker was emitted.
    """

    @pytest.mark.asyncio
    async def test_numeric_ids_also_see_the_coverage_gap(self):
        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX")

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.side_effect = locate
        mgr.get_rfc822_id.return_value = "<moved@x>"
        amap = _mock_acct_map()

        async def router(script, **kw):
            if '"by_header": true' in script:
                raise TimeoutError("recovery wedged")
            return {"updated": [], "unchanged": [], "not_found": [5]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=router,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(5)

        assert result["not_found"] == [5]
        assert "never searched" in result["hint"]
        assert "probably deleted" not in result["hint"]

    def test_an_unreadable_account_is_marked_incomplete(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(message_id=42, account="byte5").build()
        assert "allMailboxes = account.mailboxes();" in js
        assert "the account could not be read at all" in js
        # The throw must sit BEFORE any per-mailbox loop.
        assert js.index("could not be read at all") < js.index("mbLimit =")


class TestNoWriteHintClaimsDeletion:
    """Seventh round. The last hint that still asserted deletion.

    A numeric write miss searches one account and the places the index
    knows. Calling that "probably deleted" states an absence for every
    account nobody looked in — and a numeric id cannot be searched
    across accounts at all, which is precisely why the hint has to point
    at the Message-ID instead.
    """

    @pytest.mark.asyncio
    async def test_numeric_miss_does_not_say_deleted(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        mgr.get_rfc822_id.return_value = None  # nothing to recover with
        amap = _mock_acct_map()

        async def miss(script, **kw):
            return {"updated": [], "unchanged": [], "not_found": [7]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=miss,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(7, color="red")

        hint = result["hint"]
        assert "probably deleted" not in hint
        assert "not every account" in hint
        assert "message_id" in hint

    def test_no_write_hint_anywhere_asserts_deletion_unconditionally(self):
        """A guard for the whole family of hints.

        Comment lines are stripped first: they document the defects
        that were removed and legitimately contain the old wording.
        Only what the tool actually emits is checked.
        """
        import inspect

        from apple_mail_mcp import server

        code = "\n".join(
            line
            for line in inspect.getsource(server._apply_write).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "probably deleted" not in code
        # Deletion may be named in exactly one place — and only where a
        # complete search has established it.
        assert code.count("deleted") == 1
        assert "every mailbox was searched" in code


class TestFlagColoursCarryNoMeaning:
    """What a colour stands for is the user's convention, not ours.

    Everybody uses these differently — red as urgent, red as read-later,
    red as a client. A server that bakes in a scheme forces its own
    habits on every user and quietly corrupts theirs on the first
    write. The mapping here is Apple's name-to-index table and nothing
    else; the meaning lives with the user, wherever they keep their
    instructions.
    """

    SEMANTIC_WORDS = (
        "urgent",
        "important",
        "priority",
        "todo",
        "action required",
        "waiting",
        "deadline",
        "spam",
        "wichtig",
        "dringend",
    )

    def test_the_write_tool_ascribes_no_meaning(self):
        import inspect

        from apple_mail_mcp import server

        doc = (inspect.getdoc(server.set_flag) or "").lower()
        assert doc, "set_flag lost its docstring"
        for word in self.SEMANTIC_WORDS:
            assert word not in doc, (
                f"set_flag's docstring ties {word!r} to a colour. Colour "
                f"meaning is the user's convention and must stay out of "
                f"this server."
            )

    def test_the_docstring_says_so_explicitly(self):
        import inspect

        from apple_mail_mcp import server

        doc = (inspect.getdoc(server.set_flag) or "").lower()
        assert "no meaning" in doc
        assert "ask them" in doc

    def test_the_colour_table_is_apples_mapping_only(self):
        from apple_mail_mcp.builders import FLAG_COLOR_INDEX

        assert FLAG_COLOR_INDEX == {
            "red": 0,
            "orange": 1,
            "yellow": 2,
            "green": 3,
            "blue": 4,
            "purple": 5,
            "gray": 6,
        }


class TestALiveLookupSearchesTheAccountThatWasAsked:
    """The scan stops at its first hit, and the requested account was
    applied AFTERWARDS. A copy of the same message in another account
    therefore ended the search, was dropped by the filter, and the
    account the caller named — never looked at — was reported missing.
    """

    @pytest.mark.asyncio
    async def test_the_requested_account_is_the_search_scope(self):
        import apple_mail_mcp.server as srv

        seen: list[list[str]] = []

        async def fake_exec(script, timeout=None):
            # The account list is the `targets` array in the script.
            start = script.index("const targets = ") + len("const targets = ")
            seen.append(json.loads(script[start : script.index(";", start)]))
            return {"account": "Work", "mailbox": "INBOX", "id": 7}

        with (
            patch.object(srv, "execute_with_core_async", side_effect=fake_exec),
            patch.object(
                srv,
                "_visible_account_names",
                AsyncMock(return_value=["Other", "Work"]),
            ),
        ):
            await srv._locate_header_via_jxa("<a@b>", "Work")

        assert seen == [["Work"]], (
            "the live search covered accounts the caller did not ask for, "
            "and would have stopped at the first of them"
        )

    @pytest.mark.asyncio
    async def test_without_an_account_every_visible_one_is_searched(self):
        import apple_mail_mcp.server as srv

        seen: list[list[str]] = []

        async def fake_exec(script, timeout=None):
            start = script.index("const targets = ") + len("const targets = ")
            seen.append(json.loads(script[start : script.index(";", start)]))
            return {"found": False, "capped": 0, "unreadable": 0}

        with (
            patch.object(srv, "execute_with_core_async", side_effect=fake_exec),
            patch.object(
                srv,
                "_visible_account_names",
                AsyncMock(return_value=["Other", "Work"]),
            ),
        ):
            await srv._locate_header_via_jxa("<a@b>")

        assert seen == [["Other", "Work"]]


class TestAmbiguousIdsAreNeverGuessed:
    """A Mail.app id is a per-mailbox ROWID.

    The index schema's UNIQUE is `(account, mailbox, message_id)`, so the
    same number legitimately names a different message in another
    mailbox. Resolving it to one location silently picks one — for a
    WRITE that means flagging mail the caller never named.
    """

    @pytest.mark.asyncio
    async def test_an_id_in_two_mailboxes_is_not_written(self):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 2  # INBOX *and* Archive
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")
        exec_mock = AsyncMock()

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
        ):
            out = await _apply_write(
                [42], None, None, lambda g: WriteBuilder.set_read(g, True)
            )

        exec_mock.assert_not_called()  # nothing was written
        assert out["failed"] == [42]
        assert "more than one mailbox" in out["hint"]

    @pytest.mark.asyncio
    async def test_an_explicit_mailbox_resolves_the_ambiguity(self):
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        # The count answers the question it is ASKED: one message once
        # both halves of the location are pinned, two when they are not.
        mgr.count_email_locations.side_effect = (
            lambda mid, account=None, mailbox=None: 1
            if (account and mailbox)
            else 2
        )
        mgr.find_email_location.return_value = ("uuid-work", "Archive")

        acct_map = _mock_acct_map()
        acct_map.name_to_uuid.return_value = "uuid-work"

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=acct_map,
            ),
        ):
            groups, _, _, _placed, ambiguous = await _resolve_write_targets(
                [42], "Work", "Archive"
            )

        assert not ambiguous
        assert groups == [
            {"account": "Work", "mailbox": "Archive", "ids": [42]}
        ]

    @pytest.mark.asyncio
    async def test_a_mailbox_without_an_account_is_still_ambiguous(self):
        """Every account has an INBOX, and the same ROWID names a
        different message in each. Checking only when `mailbox` was
        omitted let that through to an arbitrary pick — a write to mail
        the caller never named."""
        from apple_mail_mcp.server import _resolve_write_targets

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        # Two accounts hold ROWID 42 in a mailbox called INBOX.
        mgr.count_email_locations.side_effect = (
            lambda mid, account=None, mailbox=None: 1 if account else 2
        )
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._get_account_map",
                return_value=_mock_acct_map(),
            ),
        ):
            groups, _, _, _placed, ambiguous = await _resolve_write_targets(
                [42], None, "INBOX"
            )

        assert ambiguous == [42]
        assert groups == []


class TestAMovedMessageStillHasItsAttachments:
    """`get_email(header)` asks Mail once every indexed location turns
    out stale. The attachment and link readers stopped there instead —
    so between two syncs a moved message could be read while its
    attachments and links reported that it did not exist."""

    @pytest.mark.asyncio
    async def test_the_path_resolver_falls_back_to_a_live_lookup(
        self, tmp_path
    ):
        import apple_mail_mcp.server as srv

        # Mail's account directory, with the message under its new
        # mailbox and the ROWID Mail reports.
        acct_dir = tmp_path / "UUID-WORK"
        moved = acct_dir / "Archive.mbox" / "Data" / "Messages"
        moved.mkdir(parents=True)
        emlx = moved / "77.emlx"
        emlx.write_text("stub")

        def _parse(path):
            # The recorded location now holds somebody else's message —
            # that is what "stale" means here.
            m = MagicMock()
            m.message_id_header = (
                "<a@b>" if path.name == "77.emlx" else "<stranger@x>"
            )
            return m

        acct_map = _mock_acct_map()
        acct_map.name_to_uuid.return_value = "UUID-WORK"

        mgr = MagicMock()
        # The index still points at the OLD location, and that file is
        # gone — every recorded candidate is stale.
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 12)]
        mgr.has_index.return_value = True
        mgr.find_email_path.return_value = str(
            acct_dir / "INBOX.mbox" / "Data" / "Messages" / "12.emlx"
        )

        with (
            patch.object(srv, "_get_index_manager", return_value=mgr),
            patch.object(srv, "_get_account_map", return_value=acct_map),
            patch.object(
                srv,
                "_excluded_account_names",
                return_value=set(),
            ),
            patch.object(
                srv,
                "_excluded_account_uuids",
                AsyncMock(return_value=set()),
            ),
            patch.object(
                srv,
                "_locate_header_via_jxa",
                AsyncMock(return_value=("Work", "Archive", 77)),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch("apple_mail_mcp.index.disk.parse_emlx", side_effect=_parse),
        ):
            found = await srv._resolve_emlx_path_by_header("<a@b>", None, None)

        assert found == emlx

    @pytest.mark.asyncio
    async def test_a_live_hit_with_the_wrong_header_is_refused(self, tmp_path):
        """The same rule as the indexed candidates: a ROWID is a
        hypothesis, and the file has to prove it is the right message."""
        import apple_mail_mcp.server as srv

        acct_dir = tmp_path / "UUID-WORK"
        moved = acct_dir / "Archive.mbox" / "Data" / "Messages"
        moved.mkdir(parents=True)
        (moved / "77.emlx").write_text("stub")

        stranger = MagicMock()
        stranger.message_id_header = "<someone-else@b>"

        acct_map = _mock_acct_map()
        acct_map.name_to_uuid.return_value = "UUID-WORK"

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 12)]
        mgr.find_email_path.return_value = None

        with (
            patch.object(srv, "_get_index_manager", return_value=mgr),
            patch.object(srv, "_get_account_map", return_value=acct_map),
            patch.object(
                srv,
                "_excluded_account_names",
                return_value=set(),
            ),
            patch.object(
                srv,
                "_excluded_account_uuids",
                AsyncMock(return_value=set()),
            ),
            patch.object(
                srv,
                "_locate_header_via_jxa",
                AsyncMock(return_value=("Work", "Archive", 77)),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.parse_emlx", return_value=stranger
            ),
            pytest.raises(ValueError, match="could not be located"),
        ):
            await srv._resolve_emlx_path_by_header("<a@b>", None, None)


class TestAnIncompleteScanIsNotAnAbsence:
    """A scan that could not read every mailbox has not established that
    the message is gone — the unit's own contract says so."""

    def test_the_generated_script_routes_that_to_failures(self):
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_read(
            [{"account": "A", "ids": [1], "scan": True}], True
        ).build()

        assert "could not cover every mailbox" in script
        # …and the plain not-found path still exists for the case where
        # the scan really did cover everything.
        assert "notFound.push(id)" in script


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
        mgr.count_email_locations.return_value = 1
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


class TestATimeoutIsAnUnknownOutcomeNotAFailedWrite:
    """The deadline says the ANSWER never came back, not that the write
    never happened: Mail may have applied some, all or none of the batch
    before it expired. Reporting "never reached the message" is the same
    false verdict as calling an unfinished search a missing message."""

    @pytest.mark.asyncio
    async def test_the_hint_says_unknown_and_not_never(self):
        from apple_mail_mcp.builders import WriteBuilder
        from apple_mail_mcp.server import _apply_write

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.find_email_location.return_value = ("uuid-work", "INBOX")

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
                side_effect=TimeoutError(),
            ),
        ):
            out = await _apply_write(
                [7], None, None, lambda g: WriteBuilder.set_read(g, True)
            )

        assert out["failed"] == [7]
        hint = out["hint"].lower()
        assert "unknown" in hint
        assert "never reached" not in hint, (
            "a timeout was reported as a write that did not happen"
        )
        # An empty asyncio.TimeoutError stringifies to "" — the cause
        # must not come back blank.
        assert out["error"].strip()


class TestAWriteThatWasRefusedIsNotAMissingMessage:
    """`applyByHeader` returning "failed" left the header in `remaining`,
    which then became `notFound` — an existing message reported as
    absent because Mail refused the write or it moved between reading
    the headers and mutating it."""

    def test_the_script_routes_a_refused_write_to_failures(self):
        from apple_mail_mcp.builders import WriteBuilder

        js = WriteBuilder.set_read(
            [{"account": "Work", "headers": ["<a@b>"]}], True
        ).build()

        assert "writeFailed" in js
        # The bucket decision has to consult it, not push straight to
        # notFound.
        assert "if (writeFailed.has(h))" in js
        assert "found, but the write did not take" in js


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
            (
                groups,
                not_found,
                hidden,
                _amb,
                _amb2,
            ) = await _resolve_write_targets([7], None, None)

        assert groups == []
        assert hidden == [7]

    @pytest.mark.asyncio
    async def test_naming_a_hidden_account_skips_the_whole_batch(self):
        from apple_mail_mcp.server import _resolve_write_targets

        with patch(
            "apple_mail_mcp.server._excluded_account_names",
            return_value={"Secret"},
        ):
            (
                groups,
                not_found,
                hidden,
                _amb,
                _amb2,
            ) = await _resolve_write_targets([1, 2], "Secret", None)

        assert groups == []
        assert hidden == [1, 2]


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
            (
                groups,
                not_found,
                hidden,
                _amb,
                _amb2,
            ) = await _resolve_write_targets(["<a@x>"], None, None)

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
            groups, _, _, _amb, _amb2 = await _resolve_write_targets(
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
            groups, not_found, _, _amb, _amb2 = await _resolve_write_targets(
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
            groups, _, hidden, _amb, _amb2 = await _resolve_write_targets(
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
            (
                groups,
                not_found,
                hidden,
                _amb,
                _amb2,
            ) = await _resolve_write_targets([7], None, None)

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
            groups, _, _, _amb, _amb2 = await _resolve_write_targets(
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
            groups, not_found, _, _amb, _amb2 = await _resolve_write_targets(
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
            groups, _, _, _amb, _amb2 = await _resolve_write_targets(
                [1, 2, 3], None, None
            )

        assert sorted(len(g["ids"]) for g in groups) == [1, 2]


class TestANumericStringIsAnIdNotAHeader:
    """MCP clients stringify numbers routinely. Read as a Message-ID,
    "150540" became a header nothing can hold — and a header the index
    cannot place is searched in EVERY visible account, so one mistyped
    reference turned into a full scan per account. Reported from the
    field: `set_flag(999999999)` arrived as `["999999999"]` and produced
    six failures, one per account."""

    def test_digits_become_an_int(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("999999999") == [999999999]
        assert isinstance(_normalize_message_ids("999999999")[0], int)

    def test_a_real_header_is_untouched(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids("<a@b>") == ["<a@b>"]
        assert _normalize_message_ids("a@b") == ["a@b"]

    def test_the_two_spellings_of_one_id_collapse(self):
        from apple_mail_mcp.server import _normalize_message_ids

        assert _normalize_message_ids([150540, "150540"]) == [150540]
