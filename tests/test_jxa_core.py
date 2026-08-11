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


class TestAnAccountThatCannotBeListedIsNotAnAccountWithoutTheMailbox:
    """`account.mailboxes.name()` raising is not evidence of absence.

    The catch swallowed it and left the name list empty, so the lookup
    ended in "No mailbox matching 'INBOX'. Available: " — a verdict the
    search never established. Apple Events denied, Mail quitting
    mid-call and a genuinely missing mailbox then read identically.
    """

    def _error(self, wanted):
        import json
        import subprocess

        js = f"""
var Mail = {{}};
const MailCore = {_mail_core_literal()};
const mailboxes = {{}};
Object.defineProperty(mailboxes, 'name', {{
    value: () => {{ throw new Error('-1743 not authorised'); }}
}});
mailboxes.byName = (n) => {{ throw new Error('-1728'); }};
const account = {{ mailboxes }};
try {{
    MailCore.getMailbox(account, {json.dumps(wanted)});
    console.log(JSON.stringify(null));
}} catch (e) {{ console.log(JSON.stringify(String(e.message))); }}
"""
        out = subprocess.run(
            [_node_bin(), "-e", js],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_the_error_names_the_failure_not_a_missing_mailbox(self):
        message = self._error("INBOX")
        assert message is not None
        assert "-1743" in message, message
        assert "No mailbox matching" not in message, (
            "a mailbox list that could not be read was reported as a "
            "mailbox that does not exist"
        )


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

    def test_it_wins_regardless_of_listing_order(self):
        """The first version of this test only listed the real inbox
        first, so it passed while the defect was still there — the role
        table itself matched the subfolder, because normalization drops
        the hierarchy before comparing."""
        assert (
            self._resolve(["Projects/INBOX", "Posteingang"], "INBOX")
            == "Posteingang"
        )

    def test_a_nested_folder_cannot_claim_a_role_on_its_own(self):
        """With no real inbox present, a nested lookalike must not be
        promoted into the role — the request fails loudly, naming what
        does exist, and stage 1 still matches the exact name."""
        assert self._resolve(["Projects/INBOX"], "INBOX") is None
        assert (
            self._resolve(["Projects/INBOX"], "Projects/INBOX")
            == "Projects/INBOX"
        )

    def test_a_nested_request_still_resolves_case_insensitively(self):
        """Upstream matched custom mailboxes case-insensitively, and a
        path is a custom mailbox. Comparing last segments broke it in a
        way that returns the WRONG mailbox rather than none: both
        "projects/inbox" and the account's real "INBOX" reduce to
        "inbox", so whichever Mail listed first answered."""
        assert (
            self._resolve(["INBOX", "Projects/INBOX"], "projects/inbox")
            == "Projects/INBOX"
        )
        assert (
            self._resolve(["Projects/INBOX", "INBOX"], "projects/inbox")
            == "Projects/INBOX"
        )

    def test_a_nested_request_never_falls_back_to_the_real_inbox(self):
        """Asking for a folder that does not exist must fail, not hand
        back the account's inbox because the last segments agree."""
        assert self._resolve(["INBOX"], "projects/inbox") is None

    def test_provider_hierarchy_is_still_hierarchy_we_may_ignore(self):
        """The distinction is provider prefix versus user folder:
        "[Gmail]/Sent Mail" and "INBOX.Sent" are the same mailbox under a
        different naming scheme; "Projects/INBOX" is not."""
        assert (
            self._resolve(["[Gmail]/Trash", "INBOX"], "Trash")
            == "[Gmail]/Trash"
        )
        assert self._resolve(["INBOX", "INBOX.Trash"], "Trash") == "INBOX.Trash"

    def test_provider_hierarchy_still_resolves(self):
        assert (
            self._resolve(["[Gmail]/Sent Mail", "INBOX"], "Sent Mail")
            == "[Gmail]/Sent Mail"
        )

    def test_a_nested_request_is_a_folder_name_not_a_role(self):
        """`projects/inbox` names somebody's own mailbox, even though
        its last segment normalizes to "inbox". Treating it as a role
        request made the top-level rule reject the very folder asked
        for — breaking upstream's case-insensitive custom lookup."""
        assert (
            self._resolve(["Projects/INBOX"], "projects/inbox")
            == "Projects/INBOX"
        )
        assert (
            self._resolve(["Projects/INBOX"], "PROJECTS/inbox")
            == "Projects/INBOX"
        )

    def test_a_non_role_name_still_uses_the_normalized_match(self):
        """The reorder must not break ordinary folders: `Projects` has
        no role, so shape matching is all there is."""
        assert (
            self._resolve(["INBOX", "INBOX.Projects"], "Projects")
            == "INBOX.Projects"
        )


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
