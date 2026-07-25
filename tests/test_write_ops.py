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
        assert "JSON.stringify({ updated: updated, not_found: notFound })" in (
            script
        )

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
        assert "progress" in r["problem"].lower()

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
