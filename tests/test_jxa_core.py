"""Tests for jxa/mail_core.js.

The JS runs under osascript on macOS, so it cannot be imported here.
What can be checked without Mail.app is the logic that guards a
listing: the MailCore object literal is evaluated in a plain JS engine
with stubbed message collections when node is available, and asserted
structurally otherwise.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path("src/apple_mail_mcp/jxa/mail_core.js")


def _mail_core_literal() -> str:
    body = re.search(r"const MailCore = (\{[\s\S]*?\n\});", SRC.read_text())
    assert body, "MailCore literal not found"
    return body.group(1)


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment dependent
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", f"const MailCore = {_mail_core_literal()};\n{script}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestBatchFetchDegradesPerProperty:
    """Carrying one more property must not make listings fragile.

    `standard` now fetches messageId as well, and batchFetch does one
    bulk IPC call per property — so a property Mail refuses on some
    build would otherwise take the entire listing down with it.
    """

    def test_a_refused_property_is_padded_not_fatal(self):
        out = _run_node("""
            const msgs = {
                id: () => [1, 2, 3],
                messageId: () => { throw new Error('Mail says no'); },
            };
            const r = MailCore.batchFetch(msgs, ['id', 'messageId']);
            console.log(JSON.stringify(r));
        """)
        assert out["id"] == [1, 2, 3]
        # Padded to the same length, so the caller's per-index
        # arithmetic still lines up.
        assert out["messageId"] == [None, None, None]

    def test_reading_nothing_raises_instead_of_reporting_zero(self):
        """An unreadable mailbox must never read as "0 messages"."""
        out = _run_node("""
            const msgs = { id: () => { throw new Error('no access'); } };
            let msg = null;
            try { MailCore.batchFetch(msgs, ['id']); }
            catch (e) { msg = String(e.message); }
            console.log(JSON.stringify({error: msg}));
        """)
        assert out["error"] is not None
        assert "no property could be read" in out["error"]

    def test_the_padding_is_in_the_source(self):
        """Structural check, so the guarantee is asserted even where
        node is unavailable."""
        js = _mail_core_literal()
        assert "failed.push(prop)" in js
        assert "new Array(len).fill(null)" in js
        assert "no property could be read" in js
