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
        from apple_mail_mcp.builders import WriteBuilder

        script = WriteBuilder.set_flag(
            [{"account": "W", "headers": ["<a@b>"], "by_header": True}],
            flagged=True,
        ).build()
        assert "DISCARD_MAILBOXES" in script
        for name in ("trash", "junk", "deleted messages", "spam"):
            assert f'"{name}"' in script

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

        assert set(r["not_found"]) == {1, 2}

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
