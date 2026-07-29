"""Tests for jxa/mail_core.js, exercised through node.

The JXA layer decides which mailbox a role maps to, how a Message-ID is
compared, and what happens when Apple refuses a property. None of that
needs macOS to verify: the script is evaluated with a stubbed Mail
application, so the logic is testable here.
"""

from __future__ import annotations

import pytest

from tests._mocks import mock_acct_map as _mock_acct_map
from tests._mocks import mock_index as _mock_index

__all__ = ["_mock_acct_map", "_mock_index"]


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
            ("ko", ["받은 편지함", "휴지통", "정크"], "Junk", "정크"),
            ("fi", ["Saapuneet", "Roskakori"], "INBOX", "Saapuneet"),
            ("no", ["Innboks", "Papirkurv"], "Trash", "Papirkurv"),
            ("tr", ["Gelen Kutusu", "Çöp Sepeti"], "INBOX", "Gelen Kutusu"),
            ("zh-Hant", ["收件匣", "垃圾桶"], "Trash", "垃圾桶"),
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
