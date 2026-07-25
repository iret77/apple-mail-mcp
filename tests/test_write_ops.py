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

        with pytest.raises(ValueError, match="not bool"):
            _normalize_message_ids(True)

    def test_bool_in_list_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="must be an int"):
            _normalize_message_ids([1, True, 3])

    def test_non_int_rejected(self):
        from apple_mail_mcp.server import _normalize_message_ids

        with pytest.raises(ValueError, match="must be an int"):
            _normalize_message_ids([1, "2"])

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

        async def fake_exec(script, **kw):
            return {"updated": [], "not_found": [1]}

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
    async def test_no_jxa_call_when_all_unresolvable(self):
        """If nothing resolves, no osascript invocation happens."""
        mgr = _mock_index(location=None)  # never resolves
        amap = _mock_acct_map()
        exec_mock = AsyncMock()

        with (
            patch("apple_mail_mcp.server.execute_with_core_async", exec_mock),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status([1, 2])

        exec_mock.assert_not_called()
        assert set(result["not_found"]) == {1, 2}

    @pytest.mark.asyncio
    async def test_hint_when_no_index(self):
        mgr = _mock_index(location=None, has_index=False)
        amap = _mock_acct_map()

        with (
            patch("apple_mail_mcp.server.execute_with_core_async", AsyncMock()),
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

        assert result["not_found"] == [9]
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
        assert result["not_found"] == [2]  # scan id fell through


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
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
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
