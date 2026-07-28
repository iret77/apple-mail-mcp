"""Tests for the write tools: set_flag and set_read_status.

Covers:
- WriteBuilder JXA generation (flag colors, unflag, read/unread)
- _normalize_message_ids validation (bool/empty/oversize/dedup)
- The flag-color → flag-index mapping
- Tool orchestration with mocked JXA + index (updated/not_found/
  skipped_hidden buckets, hint, read-only refusal)

Like the rest of the suite, JXA execution is mocked so these run
without macOS / Mail.app.
"""

from __future__ import annotations

import json
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ========== WriteBuilder ==========


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


# ========== _normalize_message_ids ==========


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


# ========== Flag-color mapping ==========


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
    return mgr


def _mock_acct_map(uuid_to_name="Work", excluded_uuids=None):
    m = MagicMock()
    m.ensure_loaded = AsyncMock()
    m.names_to_uuids.return_value = set(excluded_uuids or [])
    m.name_to_uuid.return_value = None
    m.uuid_to_name.return_value = uuid_to_name
    return m


# ========== set_flag / set_read_status orchestration ==========


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


class TestAutoBuildConfig:
    def test_default_true(self, monkeypatch):
        import apple_mail_mcp.config as cfg

        monkeypatch.delenv("APPLE_MAIL_INDEX_AUTO_BUILD", raising=False)
        monkeypatch.setattr(
            cfg, "CONFIG_FILE_PATH", cfg.Path("/nonexistent/config.toml")
        )
        cfg._invalidate_config_cache()
        try:
            assert cfg.get_index_auto_build() is True
        finally:
            cfg._invalidate_config_cache()

    def test_env_false(self, monkeypatch):
        import apple_mail_mcp.config as cfg

        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "false")
        assert cfg.get_index_auto_build() is False

    def test_env_true(self, monkeypatch):
        import apple_mail_mcp.config as cfg

        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "1")
        assert cfg.get_index_auto_build() is True


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


class TestIndexStatusTool:
    """get_index_status(): the agent-facing diagnostics tool."""

    def _mgr(self, *, building=False, has_index=True, indexed=100, err=None):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.cached_disk_count.return_value = None
        m.build_progress.return_value = None
        m.last_error = err
        return m

    @pytest.mark.asyncio
    async def test_reports_missing_full_disk_access(self):
        mgr = self._mgr(has_index=False, indexed=0)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("denied"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["mail_dir_accessible"] is False
        assert "Full Disk Access" in r["problem"]
        assert r["state"] == "absent"

    @pytest.mark.asyncio
    async def test_state_building(self, tmp_path):
        mgr = self._mgr(building=True, indexed=42)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "building"
        assert r["indexed_emails"] == 42
        # Building is a transient state, not a fault: reported as a
        # note plus "wait and re-check" steps.
        assert "problem" not in r
        assert "progress" in r["note"].lower()
        assert any("again" in s.lower() for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_state_empty_when_db_has_no_rows(self, tmp_path):
        mgr = self._mgr(indexed=0)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "empty"

    @pytest.mark.asyncio
    async def test_ready_reports_progress_percent(self, tmp_path):
        mgr = self._mgr(indexed=50)
        stats = MagicMock()
        stats.disk_email_count = 200
        stats.mailbox_count = 3
        stats.attachment_count = 7
        stats.db_size_mb = 1.234
        stats.failed_jobs_count = 0
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "ready"
        assert r["progress_percent"] == 25.0
        assert "problem" not in r

    @pytest.mark.asyncio
    async def test_surfaces_last_error(self, tmp_path):
        mgr = self._mgr(indexed=0, err="PermissionError: nope")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["last_error"] == "PermissionError: nope"


class TestUsableIndexAndErrorTracking:
    """has_usable_index() and last_error on the real IndexManager."""

    def test_empty_db_is_not_usable(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        m.indexed_email_count()  # creates the DB file
        assert m.has_index() is True
        assert m.has_usable_index() is False  # file exists, but no rows

    def test_sync_failure_sets_last_error_and_returns_zero(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("Cannot access"),
        ):
            assert m.sync_updates() == 0  # contract preserved
        assert "PermissionError" in (m.last_error or "")


class TestIndexModes:
    """Both supported setups: automatic (FDA) and manual (no FDA)."""

    def _mgr(self, *, indexed, building=False, has_index=True):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.last_error = None
        return m

    @pytest.mark.asyncio
    async def test_manual_mode_with_prebuilt_index_is_not_a_problem(self):
        """No FDA + index built elsewhere = working setup, not an error."""
        mgr = self._mgr(indexed=500)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("no FDA"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "ready"
        assert "problem" not in r  # not broken — just a different mode
        assert "note" in r
        assert "apple-mail-mcp index" in " ".join(r["next_steps"])

    @pytest.mark.asyncio
    async def test_no_fda_and_no_index_offers_both_paths(self):
        mgr = self._mgr(indexed=0, has_index=False)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("no FDA"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        steps = " ".join(r["next_steps"])
        assert "Full Disk Access" in r["problem"] or "Full Disk Access" in steps
        assert "apple-mail-mcp index" in steps  # both options named

    @pytest.mark.asyncio
    async def test_index_mode_reflects_auto_build_setting(
        self, tmp_path, monkeypatch
    ):
        mgr = self._mgr(indexed=0, has_index=False)
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "false")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["index_mode"] == "manual"
        # Manual mode must not promise an automatic build.
        assert "builds automatically" not in r["problem"]
        assert "apple-mail-mcp index" in " ".join(r["next_steps"])

    @pytest.mark.asyncio
    async def test_automatic_mode_mentions_self_build(
        self, tmp_path, monkeypatch
    ):
        mgr = self._mgr(indexed=0, has_index=False)
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "true")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["index_mode"] == "automatic"
        assert "automatically" in r["problem"]
        assert any("Cmd-Q" in s for s in r["next_steps"])


class TestAgentGuidance:
    """The status tool must hand the assistant usable instructions."""

    def _mgr(self, *, indexed, building=False, has_index=True):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.last_error = None
        return m

    async def _status(self, mgr, *, accessible, tmp_path=None):
        patches = [
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr)
        ]
        if accessible:
            patches.append(
                patch(
                    "apple_mail_mcp.index.disk.find_mail_directory",
                    return_value=tmp_path,
                )
            )
        else:
            patches.append(
                patch(
                    "apple_mail_mcp.index.disk.find_mail_directory",
                    side_effect=PermissionError("no FDA"),
                )
            )
        from apple_mail_mcp.server import get_index_status

        for p in patches:
            p.start()
        try:
            return await get_index_status()
        finally:
            for p in patches:
                p.stop()

    @pytest.mark.asyncio
    async def test_always_returns_user_message_and_instructions(self, tmp_path):
        r = await self._status(
            self._mgr(indexed=10), accessible=True, tmp_path=tmp_path
        )
        assert r["user_message"]
        assert "next_steps" in r or r["state"] == "ready"
        assert "assistant_instructions" in r

    @pytest.mark.asyncio
    async def test_no_fda_gives_gui_first_steps(self):
        r = await self._status(
            self._mgr(indexed=0, has_index=False), accessible=False
        )
        steps = r["next_steps"]
        # The first instruction must be a GUI action, not a command.
        assert "System Settings" in steps[0]
        assert any("Full Disk Access" in s for s in steps)

    @pytest.mark.asyncio
    async def test_telemetry_fields_present(self, tmp_path):
        r = await self._status(
            self._mgr(indexed=5), accessible=True, tmp_path=tmp_path
        )
        for key in (
            "install_mode",
            "server_version",
            "index_mode",
            "write_tools_enabled",
            "index_command",
        ):
            assert key in r, key

    def test_index_command_matches_install_mode(self, monkeypatch):
        from apple_mail_mcp.server import _index_command, _install_mode

        monkeypatch.delenv("APPLE_MAIL_MCP_LAUNCHER", raising=False)
        assert _install_mode() == "cli"
        assert _index_command() == "apple-mail-mcp index --verbose"

        monkeypatch.setenv("APPLE_MAIL_MCP_LAUNCHER", "mcpb")
        monkeypatch.delenv("APPLE_MAIL_MCP_REF", raising=False)
        assert _install_mode() == "bundle"
        # Bundle users have no CLI on PATH -> must go through uvx.
        assert _index_command().startswith("uvx --from apple-mail-mcp")

        monkeypatch.setenv("APPLE_MAIL_MCP_REF", "git+https://example/x@main")
        assert "git+https://example/x@main" in _index_command()

    def test_index_command_has_no_hardcoded_fork(self, monkeypatch):
        """Upstream-safety: no fork URL baked into the source."""
        from pathlib import Path

        import apple_mail_mcp.server as mod

        assert "iret77" not in Path(mod.__file__).read_text()


class TestRefreshIndex:
    """refresh_index(): on-demand index update."""

    def _mgr(self, *, building=False, usable=True, changes=0, err=None):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.write_lock_held.return_value = False
        m.has_usable_index.return_value = usable
        m.sync_updates.return_value = changes
        m.last_error = err
        return m

    @pytest.mark.asyncio
    async def test_incremental_sync_reports_changes(self):
        mgr = self._mgr(changes=7)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"
        assert r["changes"] == 7
        mgr.sync_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_changes_is_still_success(self):
        mgr = self._mgr(changes=0)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"
        assert "up to date" in r["message"]

    @pytest.mark.asyncio
    async def test_permission_failure_is_not_reported_as_success(self):
        """sync_updates returns 0 on FDA failure — must not read as OK."""
        mgr = self._mgr(changes=0, err="PermissionError: denied")
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "failed"
        assert r["error"] == "PermissionError: denied"
        assert r["next_steps"]

    @pytest.mark.asyncio
    async def test_refuses_while_a_build_runs(self):
        mgr = self._mgr(building=True)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "already_running"
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_rebuild_runs_detached(self):
        mgr = self._mgr()
        done = threading.Event()
        mgr.build_from_disk.side_effect = lambda *a, **k: done.set()

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "started"  # returns without waiting
        assert done.wait(timeout=5), "build thread did not run"
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_index_triggers_build_not_sync(self):
        mgr = self._mgr(usable=False)
        done = threading.Event()
        mgr.build_from_disk.side_effect = lambda *a, **k: done.set()

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "started"
        assert done.wait(timeout=5)
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_in_read_only_mode(self):
        """Refreshing the local index is not a mail mutation."""
        mgr = self._mgr(changes=1)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server.get_read_only_mode", return_value=True
            ),
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"


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


class TestLiveFlagOverlay:
    """get_email must not report stale flag state from the .emlx footer."""

    @pytest.mark.asyncio
    async def test_overlay_replaces_stale_disk_values(self, tmp_path):
        from apple_mail_mcp.server import _overlay_live_flags

        env = tmp_path / "Envelope Index"
        env.write_bytes(b"")
        result = {"read": True, "flagged": True}  # stale footer values

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=env,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_message_flags",
                return_value=(False, False),  # user cleared them in Mail
            ),
        ):
            await _overlay_live_flags(result, 42)

        assert result["flagged"] is False
        assert result["read"] is False

    @pytest.mark.asyncio
    async def test_unknown_message_leaves_values_untouched(self, tmp_path):
        from apple_mail_mcp.server import _overlay_live_flags

        env = tmp_path / "Envelope Index"
        env.write_bytes(b"")
        result = {"read": True, "flagged": True}

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=env,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_message_flags",
                return_value=None,
            ),
        ):
            await _overlay_live_flags(result, 42)

        assert result["flagged"] is True

    @pytest.mark.asyncio
    async def test_failure_is_non_fatal(self):
        """No Mail access (or any error) must not break get_email."""
        from apple_mail_mcp.server import _overlay_live_flags

        result = {"read": False, "flagged": True}
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no FDA"),
        ):
            await _overlay_live_flags(result, 42)

        assert result == {"read": False, "flagged": True}

    def test_fetch_message_flags_reads_live_columns(self, tmp_path):
        """Against a real SQLite file shaped like Apple's index."""
        import sqlite3

        from apple_mail_mcp.index.envelope_direct import fetch_message_flags

        env = tmp_path / "Envelope Index"
        conn = sqlite3.connect(env)
        conn.execute("CREATE TABLE messages (read INTEGER, flagged INTEGER)")
        conn.execute("INSERT INTO messages (read, flagged) VALUES (1, 0)")
        conn.commit()
        rowid = conn.execute("SELECT ROWID FROM messages").fetchone()[0]
        conn.close()

        assert fetch_message_flags(env, rowid) == (True, False)
        assert fetch_message_flags(env, 9999) is None


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


class TestStableMessageIdentity:
    """RFC822 Message-ID as the identity that survives moves."""

    def test_schema_v6_column_and_index_exist(self, temp_db):
        cols = {r[1] for r in temp_db.execute("PRAGMA table_info(emails)")}
        assert "rfc822_message_id" in cols
        idx = {
            r[0]
            for r in temp_db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_emails_rfc822" in idx

    def test_email_to_row_carries_the_header(self):
        from apple_mail_mcp.index.schema import email_to_row

        row = email_to_row(
            {"id": 1, "message_id_header": "<abc@example.com>"}, "acct", "INBOX"
        )
        assert "<abc@example.com>" in row

    def test_missing_header_stored_as_null_not_empty(self):
        from apple_mail_mcp.index.schema import email_to_row

        row = email_to_row({"id": 1, "message_id_header": ""}, "a", "INBOX")
        assert row[-2] is None  # NULL, so the index stays selective

    def test_migration_from_v5_adds_column(self, tmp_path):
        """A v5 database must migrate in place, not be discarded."""
        import sqlite3

        from apple_mail_mcp.index.schema import (
            SCHEMA_VERSION,
            init_database,
        )

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE emails (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                subject TEXT, sender TEXT, content TEXT,
                date_received TEXT, emlx_path TEXT,
                attachment_count INTEGER DEFAULT 0,
                indexed_at TEXT,
                UNIQUE(account, mailbox, message_id)
            );
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version (version) VALUES (5);
            INSERT INTO emails (message_id, account, mailbox, subject)
                VALUES (42, 'acct', 'INBOX', 'kept');
        """)
        conn.commit()
        conn.close()

        migrated = init_database(db)
        try:
            cols = {r[1] for r in migrated.execute("PRAGMA table_info(emails)")}
            assert "rfc822_message_id" in cols
            # Pre-existing data survives; the new column is simply NULL.
            row = migrated.execute(
                "SELECT subject, rfc822_message_id FROM emails "
                "WHERE message_id = 42"
            ).fetchone()
            assert row[0] == "kept"
            assert row[1] is None
            version = migrated.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            migrated.close()

    def test_lookup_roundtrip(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager
        from apple_mail_mcp.index.schema import INSERT_EMAIL_SQL, email_to_row

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()
        conn.execute(
            INSERT_EMAIL_SQL,
            email_to_row(
                {"id": 7, "message_id_header": "<stable@x>"}, "acct", "INBOX"
            ),
        )
        conn.commit()

        assert m.get_rfc822_id(7) == "<stable@x>"
        assert m.find_by_rfc822("<stable@x>") == [("acct", "INBOX", 7)]
        assert m.get_rfc822_id(999) is None
        assert m.find_by_rfc822("<nope@x>") == []

    def test_find_by_rfc822_returns_every_copy(self, temp_db_path):
        """The same mail can sit in several mailboxes — return all."""
        from apple_mail_mcp.index import IndexManager
        from apple_mail_mcp.index.schema import INSERT_EMAIL_SQL, email_to_row

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()
        for mid, mbox in ((1, "INBOX"), (2, "Archive")):
            conn.execute(
                INSERT_EMAIL_SQL,
                email_to_row(
                    {"id": mid, "message_id_header": "<dup@x>"}, "acct", mbox
                ),
            )
        conn.commit()

        found = m.find_by_rfc822("<dup@x>")
        assert len(found) == 2
        assert {f[1] for f in found} == {"INBOX", "Archive"}


class TestMovedMessageRecovery:
    """A write must survive another device moving the message."""

    @pytest.mark.asyncio
    async def test_recovers_via_header_and_reports_move(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
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


class TestBuildProgressVisibility:
    """During a build the user needs a percentage, not silence."""

    @pytest.mark.asyncio
    async def test_progress_reported_from_cached_disk_count(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = True
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 17_500
        mgr.cached_disk_count.return_value = 70_000
        mgr.build_progress.return_value = {
            "phase": "indexing",
            "emails_done": 17_500,
            "files_seen": 17500,
            "seconds_since_progress": 2.0,
            "appears_stalled": False,
        }
        mgr.last_error = None

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "building"
        assert r["disk_emails"] == 70_000
        assert r["progress_percent"] == 25.0
        # The expensive fresh walk must not run during a build.
        mgr.get_stats.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cached_total_still_reports_count(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = True
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 42
        mgr.cached_disk_count.return_value = None  # never walked yet
        mgr.build_progress.return_value = {
            "phase": "indexing",
            "emails_done": 42,
            "files_seen": 42,
            "seconds_since_progress": 1.0,
            "appears_stalled": False,
        }
        mgr.last_error = None

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["indexed_emails"] == 42
        assert "progress_percent" not in r  # honest: no denominator


class TestStallDetection:
    """ "Working" and "wedged" must be distinguishable."""

    def _building_mgr(self, seconds_ago, phase="indexing"):
        m = MagicMock()
        m.is_building.return_value = True
        m.write_lock_held.return_value = False
        m.has_index.return_value = True
        m.indexed_email_count.return_value = 0
        m.cached_disk_count.return_value = 70_000
        m.build_progress.return_value = {
            "phase": phase,
            "emails_done": 500,
            "files_seen": 500,
            "seconds_since_progress": seconds_ago,
            "appears_stalled": seconds_ago > 120,
        }
        m.last_error = None
        return m

    async def _status(self, mgr, tmp_path):
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            return await get_index_status()

    @pytest.mark.asyncio
    async def test_recent_progress_is_not_a_problem(self, tmp_path):
        r = await self._status(self._building_mgr(3.0), tmp_path)
        assert r["build_appears_stalled"] is False
        assert r["seconds_since_progress"] == 3.0
        assert "problem" not in r

    @pytest.mark.asyncio
    async def test_long_silence_is_reported_as_stalled(self, tmp_path):
        r = await self._status(self._building_mgr(600.0), tmp_path)
        assert r["build_appears_stalled"] is True
        assert "stuck" in r["problem"].lower()
        assert any("Cmd-Q" in s for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_reports_rows_written_even_when_count_reads_zero(
        self, tmp_path
    ):
        """The committed-batch counter proves work happened, even if the
        table count is still catching up."""
        r = await self._status(self._building_mgr(5.0), tmp_path)
        assert r["build_emails_done"] == 500


class TestOversizedEmailsAreVisible:
    """A skipped message must never be silently missing from search."""

    def test_limit_is_configurable(self, monkeypatch):
        from apple_mail_mcp.index.disk import max_emlx_size

        monkeypatch.delenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", raising=False)
        assert max_emlx_size() == 25 * 1024 * 1024

        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "100")
        assert max_emlx_size() == 100 * 1024 * 1024

    def test_too_large_detection(self, tmp_path, monkeypatch):
        from apple_mail_mcp.index.disk import emlx_too_large

        f = tmp_path / "big.emlx"
        f.write_bytes(b"x" * 2048)

        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "1")
        assert emlx_too_large(f) is False  # 2 KB under a 1 MB cap

        # 1 KB cap expressed in MB
        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", str(1 / 1024))
        assert emlx_too_large(f) is True

    def test_scan_reports_the_skip_instead_of_swallowing_it(
        self, tmp_path, monkeypatch
    ):
        from apple_mail_mcp.index import disk

        mail_dir = tmp_path
        big = tmp_path / "big.emlx"
        big.write_bytes(b"x" * 4096)

        monkeypatch.setattr(
            disk, "scan_emlx_files", lambda *a, **k: iter([big])
        )
        monkeypatch.setattr(disk, "read_envelope_index", lambda d: {})
        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", str(1 / 1024))

        skips: list = []
        results = list(
            disk.scan_all_emails(
                mail_dir, on_skip=lambda p, r: skips.append((p, r))
            )
        )

        assert results == []
        assert skips == [(big, "too_large")]

    @pytest.mark.asyncio
    async def test_status_explains_the_gap(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 63_875
        mgr.count_skipped_too_large.return_value = 4
        mgr.count_without_stable_id.return_value = 0
        mgr.last_error = None
        stats = MagicMock()
        stats.disk_email_count = 63_879
        stats.mailbox_count = 24
        stats.attachment_count = 0
        stats.db_size_mb = 1.0
        stats.failed_jobs_count = 4
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["skipped_too_large"] == 4
        # The count difference is explained, not left to guesswork.
        assert "size limit" in r["skipped_note"]
        assert "APPLE_MAIL_INDEX_MAX_EMAIL_MB" in r["skipped_note"]


class TestWarmUpPhaseIsNotMistakenForAStall:
    """Zero indexed during metadata reading is expected, not a fault."""

    def _mgr(self, phase, idle):
        m = MagicMock()
        m.is_building.return_value = True
        m.write_lock_held.return_value = False
        m.has_index.return_value = True
        m.indexed_email_count.return_value = 0
        m.cached_disk_count.return_value = 63_953
        m.build_progress.return_value = {
            "phase": phase,
            "emails_done": 0,
            "files_seen": 0,
            "seconds_since_progress": idle,
            "appears_stalled": False,
        }
        m.last_error = None
        return m

    async def _status(self, mgr, tmp_path):
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            return await get_index_status()

    @pytest.mark.asyncio
    async def test_metadata_phase_explains_the_zero(self, tmp_path):
        r = await self._status(self._mgr("reading_metadata", 150.0), tmp_path)

        assert r["build_phase"] == "reading_metadata"
        assert "problem" not in r  # 150s of no writes is normal here
        assert "warm-up" in r["user_message"]
        assert any("zero" in s.lower() for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_indexing_phase_with_zero_reads_differently(self, tmp_path):
        r = await self._status(self._mgr("indexing", 5.0), tmp_path)

        assert r["build_phase"] == "indexing"
        assert "warm-up" not in r["user_message"]

    def test_metadata_phase_gets_a_longer_grace_period(self, temp_db_path):
        """Reading metadata legitimately takes minutes; indexing must not."""
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert (
            m._STALL_SECONDS["reading_metadata"] > m._STALL_SECONDS["indexing"]
        )

    def test_heartbeat_reports_phase_and_liveness(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert m.build_progress() is None  # nothing running

        m._mark_progress("reading_metadata")
        p = m.build_progress()
        assert p["phase"] == "reading_metadata"
        assert p["emails_done"] == 0
        assert p["appears_stalled"] is False

        m._mark_progress("indexing", done=500, seen=512)
        p = m.build_progress()
        assert p["phase"] == "indexing"
        assert p["emails_done"] == 500
        assert p["files_seen"] == 512


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


class TestRefreshIndexTellsTheTruth:
    """ "started" must mean started — the bug that made a refused
    rebuild look like a running one."""

    def _mgr(self, *, busy=False):
        m = MagicMock()
        m.is_building.return_value = False
        m.write_lock_held.return_value = busy
        m.has_usable_index.return_value = True
        m.last_error = None
        return m

    @pytest.mark.asyncio
    async def test_refused_build_is_not_reported_as_started(self):
        from apple_mail_mcp.index.manager import IndexBusyError

        mgr = self._mgr()
        mgr.build_from_disk.side_effect = IndexBusyError("already running")

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "already_running"
        assert "rebuild" in r["message"].lower()

    @pytest.mark.asyncio
    async def test_crashing_build_is_reported_as_failed(self):
        mgr = self._mgr()
        mgr.build_from_disk.side_effect = OSError("disk gone")

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "failed"
        assert "OSError" in r["error"]

    @pytest.mark.asyncio
    async def test_real_start_is_confirmed_via_on_started(self):
        mgr = self._mgr()

        def build(progress_callback=None, on_started=None):
            if on_started:
                on_started()

        mgr.build_from_disk.side_effect = build

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "started"

    @pytest.mark.asyncio
    async def test_busy_lock_short_circuits_before_spawning(self):
        mgr = self._mgr(busy=True)

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "already_running"
        mgr.build_from_disk.assert_not_called()


class TestRunningSyncIsVisible:
    """A sync holds the write lock but sets no build phase, so it was
    invisible: counts frozen, last_sync stale, state 'ready'."""

    @pytest.mark.asyncio
    async def test_status_reports_a_running_sync(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = True
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 63_875
        mgr.cached_disk_count.return_value = 63_977
        mgr.build_progress.return_value = None
        mgr.last_error = None

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["sync_running"] is True
        assert "update is running" in r["user_message"]
        # Not a fault — no fresh disk walk, no "problem".
        assert "problem" not in r
        mgr.get_stats.assert_not_called()


class TestDiagnosticsAreReachable:
    """The extension's stderr is a black hole: everything a user needs
    to diagnose must come back through the status tool."""

    def test_every_build_failure_lands_in_last_error(
        self, temp_db_path, tmp_path
    ):
        """Only the unreadable-mail-dir case used to set last_error, so
        any other crash left the status reporting "no errors"."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=RuntimeError("boom mid-scan"),
            ),
            pytest.raises(RuntimeError),
        ):
            m.build_from_disk()

        assert "RuntimeError" in (m.last_error or "")
        assert "boom mid-scan" in m.last_error

    def test_failure_is_also_in_the_event_ring(self, temp_db_path, tmp_path):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            m.build_from_disk()

        events = m.recent_events()
        assert events[0]["level"] == "error"
        assert "failed" in events[0]["message"].lower()
        # The start is recorded too, so "did it even begin?" is answerable.
        assert any("started" in e["message"].lower() for e in events)

    def test_events_are_newest_first_and_bounded(self, temp_db_path):
        from apple_mail_mcp.index.manager import MAX_EVENTS, IndexManager

        m = IndexManager(db_path=temp_db_path)
        for i in range(MAX_EVENTS + 20):
            m.record_event("info", f"event {i}")

        events = m.recent_events(limit=5)
        assert len(events) == 5
        assert events[0]["message"] == f"event {MAX_EVENTS + 19}"
        assert len(m._events) == MAX_EVENTS  # ring, not a leak

    def test_recording_never_raises(self, temp_db_path):
        """Diagnostics must not be able to break what they describe."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)

        class Unprintable:
            def __repr__(self):
                raise ValueError("nope")

        m.record_event("info", "x", weird=Unprintable())  # must not raise

    @pytest.mark.asyncio
    async def test_status_carries_events_and_build_identity(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 10
        mgr.count_skipped_too_large.return_value = 0
        mgr.count_without_stable_id.return_value = 0
        mgr.build_progress.return_value = None
        mgr.last_error = None
        mgr.recent_events.return_value = [
            {
                "at": "2026-07-26T17:20:00",
                "level": "error",
                "message": "Index build failed",
                "error": "OSError: nope",
            }
        ]
        stats = MagicMock()
        stats.disk_email_count = 10
        stats.mailbox_count = 1
        stats.attachment_count = 0
        stats.db_size_mb = 1.0
        stats.failed_jobs_count = 0
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["recent_events"][0]["message"] == "Index build failed"
        assert r["server_revision"]
        assert "log_file" in r
        assert "source_ref" in r

    def test_log_path_is_configurable_and_disableable(self, monkeypatch):
        from apple_mail_mcp.config import get_log_path

        monkeypatch.delenv("APPLE_MAIL_LOG_PATH", raising=False)
        assert get_log_path().name == "server.log"

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "/tmp/custom.log")
        assert str(get_log_path()) == "/tmp/custom.log"

        # Disabled must be an explicit None: Path("") normalizes to
        # ".", which is truthy, so the old guard never fired.
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert get_log_path() is None

    def test_file_log_is_written_and_owner_only(self, tmp_path, monkeypatch):
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "sub" / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        path = _setup_file_logging()
        try:
            logging.getLogger("apple_mail_mcp.test").info("hello from test")
            for h in logging.getLogger("apple_mail_mcp").handlers:
                h.flush()

            assert path == target
            assert target.exists()
            assert "hello from test" in target.read_text()
            assert oct(target.stat().st_mode)[-3:] == "600"
        finally:
            root = logging.getLogger("apple_mail_mcp")
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)


class TestDiagnosticsDoNotLie:
    """Regressions for the review of the diagnostics pass itself."""

    def test_disabled_logging_is_reported_as_disabled(self, monkeypatch):
        from apple_mail_mcp.cli import _setup_file_logging
        from apple_mail_mcp.server import _log_file_path

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert _setup_file_logging() is None
        assert _log_file_path() == "(disabled)"

    def test_rotated_log_stays_owner_only(self, tmp_path, monkeypatch):
        """chmod once is not enough: on rollover the handler reopens the
        path with plain open(), i.e. 0644 under the usual umask."""
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        _setup_file_logging()
        root = logging.getLogger("apple_mail_mcp")
        try:
            handler = root.handlers[-1]
            handler.maxBytes = 200  # force a rollover
            log = logging.getLogger("apple_mail_mcp.rotate_test")
            for i in range(50):
                log.info("padding line %d %s", i, "x" * 40)
            handler.flush()

            assert (tmp_path / "server.log.1").exists(), "no rollover"
            for f in (target, tmp_path / "server.log.1"):
                assert oct(f.stat().st_mode)[-3:] == "600", f
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)

    def test_failed_sync_is_not_topped_by_a_success_event(self, temp_db_path):
        """The FDA path returns 0 instead of raising, so "Sync finished"
        landed on top of the ring the model is told to quote."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no FDA"),
        ):
            assert m.sync_updates() == 0

        top = m.recent_events()[0]
        assert top["level"] == "error"
        assert "finished" not in top["message"].lower()
        assert m.last_error and "PermissionError" in m.last_error

    def test_swallowed_finalize_error_is_not_reported_as_success(
        self, temp_db_path, tmp_path
    ):
        """Rows exist but FTS is empty: body search is permanently
        broken, so "finished" with last_error None hides it completely."""
        import sqlite3 as _sqlite3

        from apple_mail_mcp.index import manager as mgr_mod
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        emails = [
            {
                "id": i,
                "account": "acct",
                "mailbox": "INBOX",
                "subject": "s",
                "sender": "a@b",
                "content": "c",
                "date_received": "2026-01-01",
                "emlx_path": f"/p/{i}.emlx",
                "message_id_header": f"<{i}@x>",
                "attachments": [],
            }
            for i in range(2)
        ]

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                return_value=iter(emails),
            ),
            patch.object(
                mgr_mod,
                "rebuild_fts_index",
                side_effect=_sqlite3.OperationalError("disk full"),
            ),
        ):
            m.build_from_disk()

        assert m.last_error and "disk full" in m.last_error
        top = m.recent_events()[0]
        assert top["level"] == "error"
        assert "incomplete" in top["message"].lower()

    def test_start_event_exists_even_without_file_logging(
        self, temp_db_path, monkeypatch
    ):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "Server started", log="file logging unavailable")
        assert m.recent_events()[0]["message"] == "Server started"

    def test_event_fields_are_always_serializable(self, temp_db_path):
        import json

        from apple_mail_mcp.index.manager import IndexManager

        class Weird:
            def __str__(self):
                return "weird-value"

        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "x", obj=Weird(), n=5)
        # Must survive the JSON round-trip the MCP response performs.
        json.dumps(m.recent_events())


class TestRebuildIsDiscoverable:
    """ "Rebuild the mail index" must route here, not to Mail.app's own
    Mailbox > Rebuild — a live session sent the user there instead."""

    @pytest.mark.asyncio
    async def test_description_claims_the_rebuild_vocabulary(self):
        from apple_mail_mcp.server import mcp

        desc = (await mcp.get_tool("refresh_index")).description.lower()
        for phrase in ("rebuild", "from scratch", "re-index", "recreate"):
            assert phrase in desc, phrase

    @pytest.mark.asyncio
    async def test_description_disambiguates_from_apple_mails_index(self):
        from apple_mail_mcp.server import mcp

        # Normalize: the docstring is line-wrapped, so phrases span
        # newlines in the source.
        desc = " ".join(
            (await mcp.get_tool("refresh_index")).description.split()
        ).lower()
        assert "not apple mail's own envelope index" in desc
        assert "mailbox > rebuild" in desc
        assert "never send the user there" in desc


class TestLockErrorIsExplainedCorrectly:
    """Twice in a live session the assistant blamed Apple Mail for
    "database is locked" and told the user to quit Mail. Mail.app never
    touches this database."""

    @pytest.mark.asyncio
    async def test_guidance_names_the_real_cause(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 63_875
        mgr.count_skipped_too_large.return_value = 0
        mgr.count_without_stable_id.return_value = 0
        mgr.build_progress.return_value = None
        mgr.recent_events.return_value = []
        mgr.last_error = "OperationalError: database is locked"
        stats = MagicMock()
        stats.disk_email_count = 63_900
        stats.mailbox_count = 24
        stats.attachment_count = 0
        stats.db_size_mb = 485.0
        stats.failed_jobs_count = 0
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        steps = " ".join(r["next_steps"]).lower()
        assert "not apple mail's" in steps
        assert "quitting mail does not help" in steps
        assert "two copies" in steps
        assert (
            "apple mail has nothing to do with it" in r["user_message"].lower()
        )


class TestTimestampsAreLocal:
    """Mail.app showed 14:54 while the tool said 12:54 — stored UTC was
    handed to the reader unconverted."""

    def test_utc_is_converted_to_the_running_system_zone(self, monkeypatch):
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        # Two different zones: the conversion must follow the system,
        # never a value baked into the code.
        for tz, expected_hour in (("Europe/Berlin", 14), ("UTC", 12)):
            monkeypatch.setenv("TZ", tz)
            _time.tzset()
            out = to_local_iso("2026-07-27T12:54:00+00:00")
            assert out is not None
            assert int(out[11:13]) == expected_hour, (tz, out)

    def test_naive_values_are_read_as_utc(self, monkeypatch):
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        monkeypatch.setenv("TZ", "Europe/Berlin")
        _time.tzset()
        # Everything this server writes is UTC, so a naive string must
        # not be mistaken for local time.
        assert to_local_iso("2026-07-27T12:54:00")[11:13] == "14"

    def test_unparseable_and_empty_values_survive_untouched(self):
        from apple_mail_mcp.server import to_local_iso

        for value in ("Mon, 1 Jan 2026 10:00:00 +0100", "", None, "garbage"):
            assert to_local_iso(value) == value

    def test_dst_is_honoured(self, monkeypatch):
        """A fixed offset would be wrong for half the year."""
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        monkeypatch.setenv("TZ", "Europe/Berlin")
        _time.tzset()
        summer = to_local_iso("2026-07-27T12:00:00+00:00")
        winter = to_local_iso("2026-01-27T12:00:00+00:00")
        assert summer.endswith("+02:00")  # CEST
        assert winter.endswith("+01:00")  # CET

    @pytest.mark.asyncio
    async def test_get_email_reports_local_time(self):
        parsed = MagicMock()
        parsed.id = 42
        parsed.subject = "s"
        parsed.sender = "a@b"
        parsed.content = "c"
        parsed.date_received = "2026-07-27T12:54:00+00:00"
        parsed.date_sent = "2026-07-27T12:50:00+00:00"
        parsed.read = True
        parsed.flagged = False
        parsed.reply_to = ""
        parsed.message_id_header = "<x@y>"
        parsed.attachments = []

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_email_path.return_value = MagicMock(exists=lambda: True)
        mgr.get_email_attachments.return_value = None
        amap = _mock_acct_map()

        import time as _time

        os.environ["TZ"] = "Europe/Berlin"
        _time.tzset()
        try:
            with (
                patch(
                    "apple_mail_mcp.server._get_index_manager",
                    return_value=mgr,
                ),
                patch(
                    "apple_mail_mcp.server._get_account_map",
                    return_value=amap,
                ),
                patch(
                    "apple_mail_mcp.index.disk.parse_emlx", return_value=parsed
                ),
                patch(
                    "apple_mail_mcp.server._overlay_live_flags",
                    new_callable=AsyncMock,
                ),
            ):
                from apple_mail_mcp.server import get_email

                r = await get_email(42)
        finally:
            os.environ.pop("TZ", None)
            _time.tzset()

        assert r["date_received"].startswith("2026-07-27T14:54")
        assert r["date_sent"].startswith("2026-07-27T14:50")


class TestContentIdHeaderCrash:
    """The one file the log showed still failing after the first header
    fix: an inline image with a non-ASCII Content-ID."""

    def _emlx(self, tmp_path, mime: bytes):
        p = tmp_path / "1.emlx"
        p.write_bytes(
            f"{len(mime)}\n".encode()
            + mime
            + b"<?xml version='1.0'?><plist><dict></dict></plist>"
        )
        return p

    def test_non_ascii_content_id_parses(self, tmp_path):
        from apple_mail_mcp.index.disk import parse_emlx

        mime = (
            b"From: a@b.com\r\nSubject: t\r\n"
            b"Date: Mon, 1 Jan 2026 10:00:00 +0100\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
            b"Content-Type: text/plain\r\n\r\nbody\r\n--B\r\n"
            b"Content-Type: image/png\r\nContent-ID: <H\xe4ndler>\r\n\r\n"
            b"XX\r\n--B--\r\n"
        )
        parsed = parse_emlx(self._emlx(tmp_path, mime))
        assert parsed is not None
        assert len(parsed.attachments or []) == 1

    def test_non_ascii_attachment_filename_parses(self, tmp_path):
        from apple_mail_mcp.index.disk import parse_emlx

        mime = (
            b"From: a@b.com\r\nSubject: t\r\n"
            b"Date: Mon, 1 Jan 2026 10:00:00 +0100\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
            b"Content-Type: text/plain\r\n\r\nbody\r\n--B\r\n"
            b"Content-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; "
            b'filename="H\xe4ndler.pdf"\r\n\r\n'
            b"XX\r\n--B--\r\n"
        )
        parsed = parse_emlx(self._emlx(tmp_path, mime))
        assert parsed is not None
        assert len(parsed.attachments or []) == 1

    def test_no_raw_header_access_remains_in_the_parser(self):
        """Every header must go through header_text/_filename_text —
        two rounds of this bug came from a missed raw access."""
        from pathlib import Path as _P

        src = _P("src/apple_mail_mcp/index/disk.py").read_text()
        body = src[src.index("def parse_emlx") :]
        for pattern in ('msg["', 'part.get("Content-ID")', "get_filename()"):
            assert pattern not in body, pattern


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


class TestReadsHandOutTheStableId:
    """Every listing path must offer the handle the writes want."""

    def test_search_result_carries_message_id(self):
        from apple_mail_mcp.index.search import SearchResult

        r = SearchResult(
            id=1,
            account="uuid",
            mailbox="INBOX",
            subject="s",
            sender="f",
            content_snippet="c",
            date_received="2026-01-01",
            score=1.0,
            rfc822_message_id="<x@y.com>",
        )
        assert r.rfc822_message_id == "<x@y.com>"

    def test_jxa_listings_fetch_the_header(self):
        """The standard property set must include the stable id."""
        from apple_mail_mcp.builders import PROPERTY_SETS, QueryBuilder

        assert "message_id" in PROPERTY_SETS["standard"]
        script = QueryBuilder().from_mailbox("Work", "INBOX").build()
        assert "messageId" in script

    def test_fts_sql_selects_the_stable_column(self):
        import inspect

        from apple_mail_mcp.index import search as search_mod

        src = inspect.getsource(search_mod)
        # Both the ranked and the highlighted query, plus attachments.
        assert src.count("rfc822_message_id") >= 3


class TestBatchFetchDegradesPerProperty:
    """Carrying one more property must not make listings fragile.

    `standard` now fetches messageId as well. batchFetch does one bulk
    IPC call per property, so a property Mail refuses on some build
    would otherwise take the entire listing down.
    """

    def _mail_core(self):
        import re
        from pathlib import Path

        src = Path("src/apple_mail_mcp/jxa/mail_core.js").read_text()
        body = re.search(r"const MailCore = (\{[\s\S]*?\n\});", src)
        assert body, "MailCore literal not found"
        return body.group(1)

    def test_failing_property_is_padded_not_fatal(self):
        js = self._mail_core()
        assert "failed.push(prop)" in js
        assert "new Array(len).fill(null)" in js

    def test_total_failure_still_raises(self):
        """An unreadable mailbox must not read as 'zero messages'."""
        js = self._mail_core()
        assert "no property could be read" in js


class TestReadingByMessageIdIsVerified:
    """A stale index row must never hand back a different message.

    The header maps to a ROWID via the index, and that mapping can be
    out of date — the ROWID may by then belong to another message in
    that mailbox. Reads therefore check what came back.
    """

    @pytest.mark.asyncio
    async def test_get_email_returns_the_matching_message(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "Archiv", 42)]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            return {"id": rowid, "message_id": "<a@x.com>", "subject": "hi"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email("<a@x.com>")

        assert result["subject"] == "hi"

    @pytest.mark.asyncio
    async def test_stale_row_is_rejected_not_returned(self):
        """The ROWID now holds someone else's mail → error, not that mail."""
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 42)]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            return {
                "id": rowid,
                "message_id": "<somebody-else@x.com>",
                "subject": "NOT the requested mail",
            }

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="stale"):
                await get_email("<a@x.com>")

    @pytest.mark.asyncio
    async def test_second_candidate_is_tried(self):
        """Two indexed copies: the stale one must not end the search."""
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [
            ("uuid-work", "INBOX", 42),
            ("uuid-work", "Archiv", 77),
        ]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            header = "<a@x.com>" if rowid == 77 else "<other@x.com>"
            return {"id": rowid, "message_id": header, "subject": "found"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email("<a@x.com>")

        assert result["id"] == 77

    @pytest.mark.asyncio
    async def test_hidden_account_copy_is_never_read(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-secret", "INBOX", 1)]
        amap = _mock_acct_map(uuid_to_name="Secret")
        calls = []

        async def fake_by_id(rowid, account=None, mailbox=None):
            calls.append(rowid)
            return {"id": rowid, "message_id": "<a@x.com>"}

        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError):
                await get_email("<a@x.com>")

        assert calls == []

    @pytest.mark.asyncio
    async def test_attachment_path_verifies_the_header(self):
        """A stale row must not expose another message's attachments."""
        from types import SimpleNamespace

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 42)]
        mgr.find_email_path.return_value = "/tmp/wrong.emlx"
        amap = _mock_acct_map()

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._excluded_account_uuids",
                AsyncMock(return_value=set()),
            ),
            patch(
                "apple_mail_mcp.index.disk.parse_emlx",
                return_value=SimpleNamespace(
                    message_id_header="<somebody-else@x.com>"
                ),
            ),
        ):
            from apple_mail_mcp.server import _resolve_emlx_path

            with pytest.raises(ValueError, match="stale"):
                await _resolve_emlx_path("<a@x.com>")


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
        import os
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


class TestWellKnownMailboxesResolveByRole:
    """A mailbox name is the weakest possible handle.

    It changes with the system language ("Posteingang"), with the macOS
    version ("Eingang" in Apple's own docs), and with the provider
    ("Deleted Items", "[Gmail]/Sent Mail", "INBOX.Trash"). Resolution
    therefore goes by role, with the name table as the last stage —
    every entry in it taken from Apple's localized Mail user guide, not
    from a translation of our own.
    """

    def _mailcore_call(self, expr, names=None):
        import re
        import subprocess
        from pathlib import Path

        src = Path("src/apple_mail_mcp/jxa/mail_core.js").read_text()
        body = re.search(r"const MailCore = (\{[\s\S]*?\n\});", src).group(1)
        setup = ""
        if names is not None:
            setup = f"""
const names = {names!r};
const mailboxes = {{}};
Object.defineProperty(mailboxes, 'name', {{ value: () => names }});
mailboxes.byName = (n) => {{
    if (names.includes(n)) return {{ name: () => n }};
    throw new Error('-1728');
}};
const account = {{ mailboxes }};
"""
        js = f"var Mail = {{}};\nconst MailCore = {body};\n{setup}\n{expr}"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def _resolve(self, names, want):
        return self._mailcore_call(
            f"try {{ console.log(MailCore.getMailbox(account, {want!r}).name()); }}"
            f" catch (e) {{ console.log('MISSING'); }}",
            names=names,
        )

    @pytest.mark.parametrize(
        "label,names,want,expected",
        [
            # Language: Apple's own localized guide names.
            (
                "de",
                ["Posteingang", "Gesendet", "Papierkorb"],
                "INBOX",
                "Posteingang",
            ),
            ("de-old", ["Eingang", "Gesendet"], "INBOX", "Eingang"),
            ("fr", ["Boîte de réception", "Corbeille"], "Trash", "Corbeille"),
            ("ja", ["受信", "ゴミ箱", "迷惑メール"], "Junk", "迷惑メール"),
            ("pl", ["Przychodzące", "Kosz"], "INBOX", "Przychodzące"),
            ("ru", ["Входящие", "Корзина"], "Trash", "Корзина"),
            # Provider: hierarchy and vocabulary, not language.
            (
                "exchange",
                ["Inbox", "Sent Items", "Deleted Items", "Junk Email"],
                "Trash",
                "Deleted Items",
            ),
            (
                "gmail",
                ["INBOX", "[Gmail]/Sent Mail", "[Gmail]/Trash"],
                "Sent",
                "[Gmail]/Sent Mail",
            ),
            (
                "dovecot",
                ["INBOX", "INBOX.Sent", "INBOX.Trash"],
                "Trash",
                "INBOX.Trash",
            ),
            # Older macOS wording.
            (
                "legacy",
                ["INBOX", "Sent Messages", "Deleted Messages"],
                "Sent",
                "Sent Messages",
            ),
        ],
    )
    def test_resolution(self, label, names, want, expected):
        assert self._resolve(names, want) == expected

    def test_missing_mailbox_names_what_is_there(self):
        """A failure the caller can act on beats a bare -1728."""
        out = self._mailcore_call(
            "try { MailCore.getMailbox(account, 'Junk'); }"
            " catch (e) { console.log(String(e.message)); }",
            names=["Posteingang", "Gesendet"],
        )
        assert "Available: Posteingang, Gesendet" in out
        assert "role: junk" in out

    @pytest.mark.parametrize(
        "name,discard",
        [
            ("Papierkorb", True),
            ("[Gmail]/Spam", True),
            ("INBOX.Trash", True),
            ("Deleted Items", True),
            ("迷惑メール", True),
            ("Posteingang", False),
            ("Archiv", False),
            ("Projekte/Rechnungen", False),
        ],
    )
    def test_discard_detection(self, name, discard):
        got = self._mailcore_call(
            f"console.log(MailCore.isDiscardMailbox({name!r}));"
        )
        assert got == ("true" if discard else "false")


class TestUnsupportedLanguageDegradesUsefully:
    """A language we do not cover must not be a dead end.

    Nothing crashes: reads and search run off the index and never touch
    a mailbox name, writes report the failure with a reason, and a JXA
    listing says which mailboxes the account actually has — which is
    all a caller needs to retry correctly.
    """

    @pytest.mark.asyncio
    async def test_listing_names_the_real_mailboxes(self):
        async def turkish(*a, **kw):
            raise RuntimeError(
                'No mailbox matching "INBOX" (role: inbox). '
                "Available: Gelen Kutusu, Gönderilmiş, Çöp Kutusu"
            )

        mgr = MagicMock()
        mgr.has_index.return_value = False
        with (
            patch(
                "apple_mail_mcp.server.execute_query_async",
                side_effect=turkish,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            with pytest.raises(ValueError) as err:
                await get_emails()

        text = str(err.value)
        assert "Gelen Kutusu" in text  # the actual names are handed over
        assert "list_mailboxes" in text  # and a way to act on them
        assert "system language" in text

    @pytest.mark.asyncio
    async def test_write_reports_it_as_failed_not_as_missing_mail(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()

        async def jxa(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": [],
                "failures": [
                    {
                        "target": 5,
                        "reason": (
                            "cannot open mailbox INBOX in account Work "
                            "(No mailbox matching)"
                        ),
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

            result = await set_flag(5, color="red")

        assert result["failed"] == [5]
        assert result["not_found"] == []
        assert "Message-ID" in result["hint"]  # the name-free route


class TestFlagColourIsReadable:
    """Writing a colour you cannot read back is a one-way door.

    Reported from a live triage run: ~99 existing flags could not be
    migrated because the API exposed only flagged yes/no. Re-flagging
    blind would have destroyed the very scheme it was meant to
    preserve, so the run correctly refused to touch them.
    """

    def _flag_color_name(self, value):
        import json
        import re
        import subprocess
        from pathlib import Path

        src = Path("src/apple_mail_mcp/jxa/mail_core.js").read_text()
        body = re.search(r"const MailCore = (\{[\s\S]*?\n\});", src).group(1)
        js = (
            f"var Mail = {{}};\nconst MailCore = {body};\n"
            f"console.log(JSON.stringify("
            f"MailCore.flagColorName({json.dumps(value)})));"
        )
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip())

    @pytest.mark.parametrize(
        "index,expected",
        [
            (0, "red"),
            (1, "orange"),
            (2, "yellow"),
            (3, "green"),
            (4, "blue"),
            (5, "purple"),
            (6, "gray"),
            (-1, None),  # Apple's value for "not flagged"
            (7, None),  # out of range — never invent a colour
            (None, None),
            ("", None),
        ],
    )
    def test_index_maps_to_a_colour_name(self, index, expected):
        assert self._flag_color_name(index) == expected

    def test_null_is_not_coerced_to_red(self):
        """Number(null) is 0. Coercing would flag an unflagged mail."""
        assert self._flag_color_name(None) is None

    def test_colour_names_round_trip_with_the_write_side(self):
        from apple_mail_mcp.builders import FLAG_COLOR_INDEX

        for name, index in FLAG_COLOR_INDEX.items():
            assert self._flag_color_name(index) == name

    def test_listings_fetch_the_colour(self):
        from apple_mail_mcp.builders import PROPERTY_SETS, QueryBuilder

        assert "flag_color" in PROPERTY_SETS["standard"]
        script = QueryBuilder().from_mailbox("W", "INBOX").build()
        assert "flagIndex" in script
        assert "MailCore.flagColorName(data.flagIndex[i])" in script


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


class TestReadPathFindsUnindexedMessages:
    """The read path demanded an index row; the write path stopped
    needing one long ago.

    Live: get_emails handed out five message_ids and get_email then
    failed on every one with "not found in the index" — the messages
    had arrived after the last sync. The tool that produced the handle
    and the tool that consumes it disagreed about what a handle means.
    """

    @pytest.mark.asyncio
    async def test_falls_back_to_a_live_lookup(self):
        header = "<fresh@x.com>"
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = []  # not indexed yet
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def jxa(script, **kw):
            assert "normHeaderValue" in script
            return {
                "hit": {
                    "account": "byte5",
                    "mailbox": "Posteingang",
                    "id": 77,
                },
                "capped": 0,
                "unreadable": 0,
            }

        async def by_id(rowid, account=None, mailbox=None):
            return {"id": rowid, "message_id": header, "subject": "fresh"}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email(header)

        assert result["id"] == 77
        assert result["subject"] == "fresh"

    @pytest.mark.asyncio
    async def test_a_truly_missing_message_says_so_plainly(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def nothing(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 0}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=nothing,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="not in any visible account"):
                await get_email("<gone@x.com>")


class TestFlagColourOnTheDiskPath:
    """The colour is not in the .emlx footer, and guessing at bit
    positions would invent flags. Ask Apple for that one property."""

    @pytest.mark.asyncio
    async def test_unflagged_message_costs_no_extra_call(self):
        from apple_mail_mcp.server import _overlay_flag_color

        calls = []

        async def spy(script, **kw):
            calls.append(script)
            return {"flag_color": "red"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=spy
        ):
            result = {"flagged": False}
            await _overlay_flag_color(result, 1, "byte5", "Posteingang")

        assert calls == []
        assert "flag_color" not in result

    @pytest.mark.asyncio
    async def test_flagged_message_gets_its_colour(self):
        from apple_mail_mcp.server import _overlay_flag_color

        async def jxa(script, **kw):
            return {"flag_color": "blue"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            result = {"flagged": True}
            await _overlay_flag_color(result, 1, "byte5", "Posteingang")

        assert result["flag_color"] == "blue"


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


class TestFlagColoursComeInOneCall:
    """57 flagged messages must not mean 57 process spawns.

    A read-only survey of a flagged mailbox took a minute because each
    message needed its own osascript call just to learn its colour.
    Apple hands out a property for the whole mailbox in one bulk fetch,
    so the cost is fixed per mailbox rather than per message.
    """

    @pytest.mark.asyncio
    async def test_one_call_serves_the_whole_page(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        rows = [
            {"id": 1, "flagged": True},
            {"id": 2, "flagged": True},
            {"id": 3, "flagged": False},
            {"id": 4, "flagged": True},
        ]
        calls = []

        async def jxa(script, **kw):
            calls.append(script)
            return {"1": "orange", "2": "red", "4": "blue"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            await _overlay_flag_colors_bulk(rows, "byte5", "Posteingang")

        assert len(calls) == 1  # not one per message
        assert [r.get("flag_color") for r in rows] == [
            "orange",
            "red",
            None,
            "blue",
        ]
        # Only flagged ids are asked for.
        assert "[1, 2, 4]" in calls[0]

    @pytest.mark.asyncio
    async def test_no_flagged_message_means_no_call_at_all(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        calls = []

        async def jxa(script, **kw):
            calls.append(script)
            return {}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            await _overlay_flag_colors_bulk(
                [{"id": 1, "flagged": False}], "byte5", "Posteingang"
            )

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failing_lookup_leaves_the_listing_intact(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        async def boom(script, **kw):
            raise TimeoutError("mailbox too slow")

        rows = [{"id": 1, "flagged": True, "subject": "keep me"}]
        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=boom
        ):
            await _overlay_flag_colors_bulk(rows, "byte5", "Posteingang")

        assert rows[0]["subject"] == "keep me"
        assert "flag_color" not in rows[0]


class TestAnIncompleteSearchIsNotAVerdict:
    """Found by an external review of today's changes.

    A capped scan, an unreadable mailbox, a timeout or a refused
    Automation permission all leave the question open. Answering them
    with "most likely deleted" turns an incomplete search into a
    statement about the user's mail — the same defect that, in its
    write-path form, cost a full day of debugging.
    """

    def _ctx(self, jxa):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]
        return (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        )

    @pytest.mark.asyncio
    async def test_capped_scan_says_the_search_was_incomplete(self):
        async def capped(script, **kw):
            return {"hit": None, "capped": 12, "unreadable": 0}

        a, b, c = self._ctx(capped)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        text = str(err.value)
        assert "could not be completed" in text
        assert "12 mailbox" in text
        assert "deleted" not in text  # never a verdict

    @pytest.mark.asyncio
    async def test_unreadable_mailbox_is_not_a_missing_message(self):
        async def unreadable(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 3}

        a, b, c = self._ctx(unreadable)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="could not be completed"):
                await get_email("<x@y.com>")

    @pytest.mark.asyncio
    async def test_a_refused_apple_event_is_not_a_missing_message(self):
        async def refused(script, **kw):
            raise RuntimeError("Not authorized to send Apple events (-1743)")

        a, b, c = self._ctx(refused)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        assert "-1743" in str(err.value)
        assert "says nothing about whether the message exists" in str(err.value)

    @pytest.mark.asyncio
    async def test_a_complete_search_may_still_conclude_absence(self):
        """The distinction only helps if a real miss stays a real miss."""

        async def clean_miss(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 0}

        a, b, c = self._ctx(clean_miss)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        assert "every mailbox was searched" in str(err.value)


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


class TestRecoveryAndStrategy3KeepTheirGaps:
    """Third review round, same property, two more places.

    Stable-id recovery discarded its own scan counters and its failures,
    and get_email's all-mailbox scan threw a bare "not found" whether it
    had searched everything or given up at the cap. Both let an
    unfinished search read as proof the message is gone.
    """

    def test_strategy3_scan_reports_what_it_skipped(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(message_id=42, account="byte5").build()
        assert "let unsearched = Math.max(0, allMailboxes.length" in js
        assert "unsearched++" in js
        assert "INCOMPLETE:" in js

    @pytest.mark.asyncio
    async def test_incomplete_strategy3_does_not_claim_absence(self):
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def incomplete(script, **kw):
            raise RuntimeError(
                "Error: Message not found with ID: 42 "
                "(INCOMPLETE: 9 mailbox(es) not searched)"
            )

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=incomplete,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "search was incomplete" in text
        assert "9 mailbox" in text
        assert "does not mean the message is gone" in text

    @pytest.mark.asyncio
    async def test_a_failed_recovery_counts_as_unsearched(self):
        """Recovery that never ran must not harden a miss into a fact."""

        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX")

        mgr = MagicMock()
        mgr.has_index.return_value = True
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
        assert result["diagnostics"]["mailboxes_not_searched"] >= 1


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


class TestAStalePathIsNotADeletedMessage:
    """Fifth review round, the last one.

    A missing .emlx file means the path recorded in the index is wrong.
    It does not mean the message left Apple Mail — it may have been
    re-filed, or Mail may have rebuilt its store. The old code raised
    "deleted or moved" right there and skipped the live strategies,
    justified by the assumption that they would fail anyway. That
    assumption was never verified.
    """

    def test_the_shortcut_is_gone(self):
        import inspect

        from apple_mail_mcp import server

        src = inspect.getsource(server)
        assert "deleted or moved since the last" not in src
        assert "KEEP GOING" in src


class TestNumericIdCannotSpeakForOtherAccounts:
    """Sixth round. Strategy 3 walks ONE account.

    With six accounts configured, "message not found" after searching
    one of them is an absence claim about five that nobody looked in.
    A numeric id is only unique within a mailbox, so it cannot be
    searched across accounts at all — which makes saying so, and
    pointing at the Message-ID, the only honest answer.
    """

    @pytest.mark.asyncio
    async def test_says_which_accounts_were_not_searched(self):
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def gone(script, **kw):
            raise RuntimeError("Error: Message not found with ID: 42")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=gone,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="iCloud"),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["iCloud", "byte5", "freenea"]),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "2 account(s) were not searched" in text
        assert "Message-ID" in text  # the handle that does span accounts

    @pytest.mark.asyncio
    async def test_a_single_account_setup_still_says_not_found(self):
        """With one account, one account IS everywhere."""
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def gone(script, **kw):
            raise RuntimeError("Error: Message not found with ID: 42")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=gone,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["byte5"]),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        assert "not searched" not in str(err.value)
