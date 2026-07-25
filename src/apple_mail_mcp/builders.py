"""
JXA Script Builders for Apple Mail operations.

These builders generate optimized JXA scripts that use batch property
fetching for maximum performance.
"""

import json
from dataclasses import dataclass, field

# Standard email properties available for batch fetching
EMAIL_PROPERTIES = {
    "id": "id",
    "subject": "subject",
    "sender": "sender",
    "date_received": "dateReceived",
    "date_sent": "dateSent",
    "read": "readStatus",
    "flagged": "flaggedStatus",
    "deleted": "deletedStatus",
    "junk": "junkMailStatus",
    "reply_to": "replyTo",
    "message_id": "messageId",
    "source": "source",  # Raw email source - expensive!
}

# Shorthand aliases for common property sets
PROPERTY_SETS = {
    "minimal": ["id", "subject", "sender", "date_received"],
    "standard": [
        "id",
        "subject",
        "sender",
        "date_received",
        "read",
        "flagged",
    ],
    "full": [
        "id",
        "subject",
        "sender",
        "date_received",
        "date_sent",
        "read",
        "flagged",
        "reply_to",
        "message_id",
    ],
}


@dataclass
class QueryBuilder:
    """
    Builder for constructing optimized email query scripts.

    Uses batch property fetching for fast execution. Supports filtering,
    limiting, and property selection.

    Example:
        query = (QueryBuilder()
            .from_mailbox("Work", "INBOX")
            .select("sender", "subject", "date_received", "read")
            .where("data.dateReceived[i] >= MailCore.today()")
            .limit(50)
            .build())
    """

    _account: str | None = None
    _mailbox: str = "INBOX"
    _properties: list[str] = field(default_factory=list)
    _filter_expr: str | None = None
    _limit: int | None = None
    _order_by: str | None = None
    _descending: bool = True

    def from_mailbox(
        self, account: str | None = None, mailbox: str = "INBOX"
    ) -> "QueryBuilder":
        """
        Set the source mailbox for the query.

        Args:
            account: Account name (None for first/default account)
            mailbox: Mailbox name (default: "INBOX")
        """
        self._account = account
        self._mailbox = mailbox
        return self

    def select(self, *props: str) -> "QueryBuilder":
        """
        Select properties to fetch.

        Use property names like: id, subject, sender, date_received,
        read, flagged, etc. Or use a preset: "minimal", "standard", "full".

        Args:
            props: Property names or preset names
        """
        for prop in props:
            if prop in PROPERTY_SETS:
                self._properties.extend(PROPERTY_SETS[prop])
            elif prop in EMAIL_PROPERTIES:
                self._properties.append(prop)
            else:
                raise ValueError(
                    f"Unknown property: {prop}. "
                    f"Valid: {list(EMAIL_PROPERTIES.keys())}"
                )
        return self

    def where(self, js_expression: str) -> "QueryBuilder":
        """
        Add a filter expression (JavaScript).

        The expression has access to:
        - `data`: Object with arrays of fetched properties
        - `i`: Current index in the loop
        - `MailCore`: The MailCore utilities

        Example:
            .where("data.dateReceived[i] >= MailCore.today()")
            .where("data.subject[i].toLowerCase().includes('urgent')")

        Args:
            js_expression: JavaScript boolean expression
        """
        self._filter_expr = js_expression
        return self

    def limit(self, n: int) -> "QueryBuilder":
        """Limit the number of results."""
        self._limit = n
        return self

    def order_by(self, prop: str, descending: bool = True) -> "QueryBuilder":
        """
        Order results by a property.

        Args:
            prop: Property name to sort by
            descending: Sort descending (default: True, newest first)
        """
        if prop not in EMAIL_PROPERTIES:
            raise ValueError(f"Unknown property for ordering: {prop}")
        self._order_by = prop
        self._descending = descending
        return self

    def build(self) -> str:
        """
        Generate the JXA script.

        Returns:
            JavaScript code that uses MailCore and returns JSON
        """
        if not self._properties:
            # Default to standard properties
            self._properties = PROPERTY_SETS["standard"].copy()

        # Remove duplicates while preserving order
        props = list(dict.fromkeys(self._properties))

        # Map Python property names to JXA property names
        jxa_props = [EMAIL_PROPERTIES[p] for p in props]

        # Build the script
        account_json = json.dumps(self._account)
        mailbox_json = json.dumps(self._mailbox)
        props_json = json.dumps(jxa_props)

        lines = [
            "// Setup",
            f"const account = MailCore.getAccount({account_json});",
            f"const mailbox = MailCore.getMailbox(account, {mailbox_json});",
            "const msgs = mailbox.messages;",
            "",
            "// Batch fetch (optimized - single IPC per property)",
            f"const data = MailCore.batchFetch(msgs, {props_json});",
            "",
            "// Build results",
            "const results = [];",
            f"const len = data.{jxa_props[0]}.length;",
            "",
        ]

        # Loop with optional limit
        if self._limit:
            loop_cond = f"i < len && results.length < {self._limit}"
            lines.append(f"for (let i = 0; {loop_cond}; i++) {{")
        else:
            lines.append("for (let i = 0; i < len; i++) {")

        # Optional filter
        if self._filter_expr:
            lines.append(f"    if (!({self._filter_expr})) continue;")

        # Build result object
        lines.append("    results.push({")
        for py_name, jxa_name in zip(props, jxa_props, strict=True):
            if jxa_name in ("dateReceived", "dateSent"):
                fmt = f"MailCore.formatDate(data.{jxa_name}[i])"
                lines.append(f"        {py_name}: {fmt},")
            else:
                lines.append(f"        {py_name}: data.{jxa_name}[i],")
        lines.append("    });")
        lines.append("}")

        # Optional sorting (in JS after collection)
        if self._order_by:
            direction = -1 if self._descending else 1
            lines.append("")
            lines.append("// Sort results")
            lines.append("results.sort((a, b) => {")
            lines.append(f"    const va = a.{self._order_by};")
            lines.append(f"    const vb = b.{self._order_by};")
            lines.append(f"    if (va < vb) return {-direction};")
            lines.append(f"    if (va > vb) return {direction};")
            lines.append("    return 0;")
            lines.append("});")

        lines.append("")
        lines.append("JSON.stringify(results);")

        return "\n".join(lines)


@dataclass
class AccountsQueryBuilder:
    """Builder for listing accounts and mailboxes."""

    def list_accounts(self) -> str:
        """Generate script to list all mail accounts."""
        return "JSON.stringify(MailCore.listAccounts());"

    def list_mailboxes(self, account: str | None = None) -> str:
        """Generate script to list mailboxes for an account."""
        account_json = json.dumps(account)
        return f"""
const account = MailCore.getAccount({account_json});
JSON.stringify(MailCore.listMailboxes(account));
"""


# Apple Mail flag-color name → `flag index` value. Setting `flagIndex`
# to one of these also flags the message; `flaggedStatus = false`
# clears it (resetting the index to -1). "default" (a plain flag with
# no forced color) and "none" (unflag) are handled by the caller, not
# by this map.
FLAG_COLOR_INDEX = {
    "red": 0,
    "orange": 1,
    "yellow": 2,
    "green": 3,
    "blue": 4,
    "purple": 5,
    "gray": 6,
}


@dataclass
class WriteBuilder:
    """Builder for batch property writes (read/flag) over located messages.

    Applies a single property change to a batch of messages grouped by
    ``(account, mailbox)`` in ONE osascript invocation. Each group opens
    its mailbox once and batch-fetches the id array (``mb.messages.id()``)
    rather than doing per-message IPC — the same 87x optimization the read
    path uses.

    The mutating JS statement (``apply_js``) is derived solely from the
    operation and a validated int/bool — never from caller-supplied
    strings. Account/mailbox names and message ids cross into JXA only
    through ``json.dumps`` of ``groups``, so nothing untrusted is
    interpolated into executable JS.

    Two group shapes are accepted:

    - **Located** — ``{"account", "mailbox", "ids"}``: open that mailbox
      once and apply. Fast; used when the index (or an explicit hint)
      knows where an id lives.
    - **Scan** — ``{"account", "ids", "scan": true}`` (no mailbox):
      iterate the account's mailboxes (bounded by ``max_scan_mailboxes``)
      looking for each id and apply on match. The index-free fallback,
      mirroring ``get_email``'s all-mailbox scan.

    Construct via the :meth:`set_read` / :meth:`set_flag` factories.

    Args:
        groups: list of located and/or scan group dicts (see above).
        apply_js: JS run per located message, with ``msg`` in scope.
        max_scan_mailboxes: cap on mailboxes visited per scan group.
    """

    groups: list[dict]
    apply_js: str
    max_scan_mailboxes: int = 50

    @classmethod
    def set_read(
        cls, groups: list[dict], read: bool, max_scan_mailboxes: int = 50
    ) -> "WriteBuilder":
        """Build a read/unread (seen/unseen) writer."""
        return cls(
            groups,
            f"msg.readStatus = {'true' if read else 'false'};",
            max_scan_mailboxes,
        )

    @classmethod
    def set_flag(
        cls,
        groups: list[dict],
        flagged: bool,
        flag_index: int | None = None,
        max_scan_mailboxes: int = 50,
    ) -> "WriteBuilder":
        """Build a flag/unflag writer.

        Args:
            flagged: ``False`` clears the flag (color reset to -1).
            flag_index: When ``flagged`` and this is 0-6, force that flag
                color; ``None`` flags without forcing a color.
        """
        if not flagged:
            apply_js = "msg.flaggedStatus = false;"
        elif flag_index is None:
            apply_js = "msg.flaggedStatus = true;"
        else:
            apply_js = (
                f"msg.flaggedStatus = true; msg.flagIndex = {int(flag_index)};"
            )
        return cls(groups, apply_js, max_scan_mailboxes)

    def build(self) -> str:
        """Generate the JXA script string.

        Returns a script that ends in ``JSON.stringify({updated, not_found})``
        where both are arrays of message ids. Ids present in a group but
        absent from the mailbox (moved/deleted since indexing) land in
        ``not_found`` rather than failing the whole batch.
        """
        groups_json = json.dumps(self.groups)
        max_scan = int(self.max_scan_mailboxes)
        return f"""
const groups = {groups_json};
const MAX_SCAN = {max_scan};
const updated = [];
const notFound = [];

// Apply the change to the message at `idx`, but only after confirming
// it really is `targetId`. The id list is a snapshot: if the mailbox
// changes between fetching it and writing (mail arrives, a message is
// moved or deleted), positions shift and the same index would point at
// a different message — silently modifying the wrong mail. On a
// mismatch, re-resolve by id and skip if that fails.
function applyToMessage(collection, idx, targetId) {{
    let msg = collection[idx];
    try {{
        if (msg.id() !== targetId) {{
            msg = collection.byId(targetId);
            if (msg.id() !== targetId) return false;
        }}
    }} catch (e) {{
        return false;
    }}
    {self.apply_js}
    return true;
}}

for (const g of groups) {{
    let account;
    try {{
        account = MailCore.getAccount(g.account);
    }} catch (e) {{
        for (const id of g.ids) notFound.push(id);
        continue;
    }}

    if (g.scan) {{
        // No known mailbox: scan the account's mailboxes for each id.
        let mailboxes;
        try {{
            mailboxes = account.mailboxes();
        }} catch (e) {{
            for (const id of g.ids) notFound.push(id);
            continue;
        }}
        const remaining = new Set(g.ids);
        const limit = Math.min(mailboxes.length, MAX_SCAN);
        for (let m = 0; m < limit && remaining.size > 0; m++) {{
            let ids;
            try {{
                ids = mailboxes[m].messages.id();
            }} catch (e) {{
                continue;  // skip inaccessible mailbox (Junk/Drafts -1728)
            }}
            for (const targetId of Array.from(remaining)) {{
                const idx = ids.indexOf(targetId);
                if (idx === -1) continue;
                remaining.delete(targetId);
                try {{
                    if (applyToMessage(mailboxes[m].messages, idx, targetId)) {{
                        updated.push(targetId);
                    }} else {{
                        notFound.push(targetId);
                    }}
                }} catch (e) {{
                    notFound.push(targetId);
                }}
            }}
        }}
        for (const id of remaining) notFound.push(id);
        continue;
    }}

    // Located group: open the known mailbox directly.
    let mailbox;
    try {{
        mailbox = MailCore.getMailbox(account, g.mailbox);
    }} catch (e) {{
        for (const id of g.ids) notFound.push(id);
        continue;
    }}

    let ids;
    try {{
        ids = mailbox.messages.id();
    }} catch (e) {{
        for (const id of g.ids) notFound.push(id);
        continue;
    }}

    for (const targetId of g.ids) {{
        const idx = ids.indexOf(targetId);
        if (idx === -1) {{
            notFound.push(targetId);
            continue;
        }}
        try {{
            if (applyToMessage(mailbox.messages, idx, targetId)) {{
                updated.push(targetId);
            }} else {{
                notFound.push(targetId);
            }}
        }} catch (e) {{
            notFound.push(targetId);
        }}
    }}
}}

JSON.stringify({{ updated: updated, not_found: notFound }});
"""


@dataclass
class GetEmailBuilder:
    """Builder for Strategy 3: find a single email by iterating mailboxes.

    Generates a JXA script that iterates up to ``max_mailboxes``
    mailboxes looking for an email by ID, then returns its full
    content with attachments.
    """

    message_id: int
    account: str | None = None
    max_mailboxes: int = 50
    attachment_js: str = ""

    def build(self) -> str:
        """Generate the JXA script string."""
        acct_setup = (
            f"const account = Mail.accounts.byName({json.dumps(self.account)});"
            if self.account
            else "const account = Mail.accounts[0];"
        )
        return f"""
const targetId = {self.message_id};
let msg = null;
{acct_setup}

const allMailboxes = account.mailboxes();
const mbLimit = Math.min(allMailboxes.length, {self.max_mailboxes});
for (let i = 0; i < mbLimit && !msg; i++) {{
    try {{
        const mb = allMailboxes[i];
        const mbIds = mb.messages.id();
        const mbIdx = mbIds.indexOf(targetId);
        if (mbIdx !== -1) {{
            msg = mb.messages[mbIdx];
        }}
    }} catch(e) {{
        // Skip inaccessible mailboxes (Junk/Drafts -1728)
    }}
}}

if (!msg) {{
    throw new Error('Message not found with ID: ' + targetId);
}}

{self.attachment_js}

JSON.stringify({{
    id: msg.id(),
    subject: msg.subject(),
    sender: msg.sender(),
    content: msg.content(),
    date_received: MailCore.formatDate(msg.dateReceived()),
    date_sent: MailCore.formatDate(msg.dateSent()),
    read: msg.readStatus(),
    flagged: msg.flaggedStatus(),
    reply_to: msg.replyTo(),
    message_id: msg.messageId(),
    attachments: attachments
}});
"""
