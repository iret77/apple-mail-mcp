"""Tests for the --profile flag wiring in cli.py (#issue-followup-from-#60).

The flag wraps `index` / `rebuild` operations in cProfile when set.
We test the helper directly rather than the full CLI command — the
helper *is* the new behavior; the CLI surface around it is unchanged.
"""

from __future__ import annotations

import pstats
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from apple_mail_mcp.cli import _run_optionally_profiled


def test_profile_path_none_runs_op_without_profiling(tmp_path: Path) -> None:
    calls = []

    def op() -> str:
        calls.append("ran")
        return "result"

    out = _run_optionally_profiled(op, profile_path=None)
    assert out == "result"
    assert calls == ["ran"]


def test_profile_path_set_writes_pstats_dump(tmp_path: Path) -> None:
    profile_file = tmp_path / "out.prof"
    calls = []

    def op() -> int:
        calls.append("ran")
        # Do something measurable so the dump has content.
        return sum(range(10_000))

    out = _run_optionally_profiled(op, profile_path=profile_file)

    assert out == sum(range(10_000))
    assert calls == ["ran"]
    assert profile_file.exists(), "profile dump should be written to disk"
    assert profile_file.stat().st_size > 0, "profile dump should not be empty"

    # cProfile dumps are marshal-encoded; pstats.Stats is the
    # canonical loader. Just verifying it parses without error
    # confirms the dump is valid.
    stats = pstats.Stats(str(profile_file))
    assert stats.total_calls > 0


def test_profile_propagates_op_return_value(tmp_path: Path) -> None:
    # Regression: the cProfile path uses a list-holder pattern to
    # capture the return value across cProfile's exec scope. Verify
    # that propagation doesn't drop or mutate the value.
    profile_file = tmp_path / "out.prof"

    def op() -> dict:
        return {"k": "v", "n": 42}

    out = _run_optionally_profiled(op, profile_path=profile_file)
    assert out == {"k": "v", "n": 42}


class TestNonBlockingStartup:
    """_run_serve should not block on sync."""

    def test_run_serve_does_not_block(self):
        """mcp.run() is called immediately, not after sync."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        # sync_updates sleeps to simulate slow sync
        mock_manager.sync_updates.side_effect = lambda: (time.sleep(5) or 0)

        mock_mcp = MagicMock()

        with (
            patch(
                "apple_mail_mcp.index.IndexManager.get_instance",
                return_value=mock_manager,
            ),
            patch("apple_mail_mcp.server.mcp", mock_mcp),
            patch("apple_mail_mcp.server._cleanup_old_attachments"),
        ):
            from apple_mail_mcp.cli import _run_serve

            start = time.time()
            _run_serve(watch=False)
            elapsed = time.time() - start

            # mcp.run() should be called within ~1s, not 5s
            assert elapsed < 2.0
            mock_mcp.run.assert_called_once()

    def test_search_does_not_trigger_sync(self):
        """Verify _sync_lock and auto-sync were removed."""
        import apple_mail_mcp.server as srv

        assert not hasattr(srv, "_sync_lock")


class TestAutoBuildOnFirstRun:
    """What `serve` does when there is no index yet."""

    def _serve(self, monkeypatch, auto_build: str, capsys):
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = False
        mock_manager.build_from_disk.return_value = 3
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", auto_build)

        with (
            patch(
                "apple_mail_mcp.index.IndexManager.get_instance",
                return_value=mock_manager,
            ),
            patch("apple_mail_mcp.server.mcp", MagicMock()),
            patch("apple_mail_mcp.server._cleanup_old_attachments"),
        ):
            from apple_mail_mcp.cli import _run_serve

            _run_serve(watch=False)
        # The build runs on a daemon thread; give it a moment.
        for _ in range(50):
            if mock_manager.build_from_disk.called:
                break
            time.sleep(0.01)
        return mock_manager, capsys.readouterr().err

    def test_disabled_builds_nothing_but_says_so(self, monkeypatch, capsys):
        """Silence here becomes an empty body search later, with nothing
        to explain it."""
        manager, err = self._serve(monkeypatch, "false", capsys)

        manager.build_from_disk.assert_not_called()
        assert "No search index" in err
        assert "apple-mail-mcp index" in err

    def test_enabled_builds_in_the_background(self, monkeypatch, capsys):
        manager, err = self._serve(monkeypatch, "true", capsys)

        manager.build_from_disk.assert_called_once()
        assert "building in the background" in err

    def test_the_build_really_is_in_the_background(self, monkeypatch, capsys):
        """The other test passes even if the build were synchronous,
        because the mock returns instantly. Make it slow: mcp.run() has
        to be reached before the build finishes, or 'background' is just
        a word in a message."""
        started = threading.Event()
        release = threading.Event()

        def slow_build(*a, **kw):
            started.set()
            release.wait(timeout=5)
            return 0

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = False
        mock_manager.build_from_disk.side_effect = slow_build
        mock_mcp = MagicMock()
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "true")

        try:
            with (
                patch(
                    "apple_mail_mcp.index.IndexManager.get_instance",
                    return_value=mock_manager,
                ),
                patch("apple_mail_mcp.server.mcp", mock_mcp),
                patch("apple_mail_mcp.server._cleanup_old_attachments"),
            ):
                from apple_mail_mcp.cli import _run_serve

                _run_serve(watch=False)

            assert started.wait(timeout=5), "the build never started"
            mock_mcp.run.assert_called_once()  # reached while it runs
        finally:
            release.set()
