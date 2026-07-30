"""Tests for jxa/mail_core.js, exercised through node.

Which mailbox a role maps to is decided in the JXA layer. That does not
need macOS to verify: the script is evaluated with a stubbed Mail
application, so the resolution order is testable here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path("src/apple_mail_mcp/jxa/mail_core.js")


def _node_bin() -> str:
    """The `node` binary, or skip.

    These tests evaluate the JXA layer in a plain JS engine so they run
    without macOS. That trades one dependency for another, so a machine
    without node skips them rather than reporting a failure it cannot
    act on.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment dependent
        pytest.skip("node not available")
    return node


def _mail_core_literal() -> str:
    """The MailCore object literal, for evaluation in a plain JS engine.

    Anchored on the closing brace in column 0 rather than on the first
    "\n});", which sits inside a nested function and would silently
    return half an object.
    """
    body = re.search(
        r"const MailCore = (\{[\s\S]*?^\};)", SRC.read_text(), re.M
    )
    assert body, "MailCore literal not found"
    return body.group(1)


class TestMailCoreIsIntact:
    """Every method the Python side calls must actually be on the object.

    A JS object literal absorbs mistakes in silence: one unterminated
    JSDoc block comments out the method that follows it, and the file
    still parses. The result is `MailCore.batchFetch is not a function`
    at runtime on macOS — where no test runs. So the surface is asserted
    here instead of assumed.
    """

    # Everything server.py / builders.py reach for through MailCore.
    REQUIRED = (
        "getAccount",
        "getMailbox",
        "batchFetch",
        "getMessageIds",
        "getMessageById",
        "isDiscardMailbox",
        "mailboxRole",
        "normalizeMailboxName",
        "specialMailbox",
        "safely",
        "today",
        "daysAgo",
        "formatDate",
        "listAccounts",
        "listMailboxes",
    )

    def test_every_required_method_survives_evaluation(self):
        js = (
            f"const MailCore = {_mail_core_literal()};\n"
            "console.log(Object.keys(MailCore).join(','));"
        )
        out = subprocess.run(
            [_node_bin(), "-e", js],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        present = set(out.stdout.strip().split(","))

        missing = [name for name in self.REQUIRED if name not in present]
        assert not missing, (
            f"MailCore is missing {missing}. A method swallowed by an "
            f"unterminated comment block parses fine and fails only on "
            f"macOS at call time."
        )


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
        out = subprocess.run(
            [_node_bin(), "-e", js], capture_output=True, text=True
        )
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


class TestARoleRequestIsNotAnsweredByAUserFolder:
    """Order matters between the role table and the normalized match.

    Normalization drops provider hierarchy, so a user's own
    `Projects/INBOX` normalizes to "inbox". With the generic match
    running first, a German account holding `Posteingang` **and**
    `Projects/INBOX` answered a request for the inbox with the
    subfolder — the wrong mailbox, silently, on a read or a write.
    """

    def _resolve(self, names, wanted):
        import json
        import subprocess

        js = f"""
var Mail = {{}};
const MailCore = {_mail_core_literal()};
const names = {json.dumps(names)};
const mailboxes = {{}};
Object.defineProperty(mailboxes, 'name', {{ value: () => names }});
mailboxes.byName = (n) => {{
    if (names.includes(n)) return {{ name: () => n }};
    throw new Error('-1728');
}};
const account = {{ mailboxes }};
try {{
    console.log(JSON.stringify(
        String(MailCore.getMailbox(account, {json.dumps(wanted)}).name())
    ));
}} catch (e) {{ console.log(JSON.stringify(null)); }}
"""
        out = subprocess.run(
            [_node_bin(), "-e", js],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_a_localized_inbox_wins_over_a_lookalike_subfolder(self):
        assert (
            self._resolve(["Posteingang", "Projects/INBOX"], "INBOX")
            == "Posteingang"
        )

    def test_provider_hierarchy_still_resolves(self):
        assert (
            self._resolve(["[Gmail]/Sent Mail", "INBOX"], "Sent Mail")
            == "[Gmail]/Sent Mail"
        )

    def test_a_non_role_name_still_uses_the_normalized_match(self):
        """The reorder must not break ordinary folders: `Projects` has
        no role, so shape matching is all there is."""
        assert (
            self._resolve(["INBOX", "INBOX.Projects"], "Projects")
            == "INBOX.Projects"
        )
