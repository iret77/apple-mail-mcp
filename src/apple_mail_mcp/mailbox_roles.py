"""Well-known mailbox roles, for the Python side of the server.

A mailbox NAME is the weakest handle there is: it changes with the
system language, the macOS version and the provider. The JXA paths
resolve well-known mailboxes by role in `MailCore.getMailbox()`
(`jxa/mail_core.js`); the Envelope Index fast path needs the same
answer without a round-trip to Mail, so this module mirrors the role
table and the three helpers it rests on.

`jxa/mail_core.js` is the reference. A test runs that file through node
and fails when the table or any helper disagrees with this one, so a
name added on one side and forgotten on the other cannot ship.
"""

from __future__ import annotations

import re

# Same entries, same order as MailCore.MAILBOX_ROLES. Every entry is
# sourced from Apple's localized Mail user guide or is a documented
# provider/legacy name — see the comments in mail_core.js.
MAILBOX_ROLES: dict[str, tuple[str, ...]] = {
    "inbox": (
        "INBOX",
        "Inbox",
        "In",
        "Eingang",
        "Posteingang",
        "Boîte de réception",
        "Entrada",
        "Entrata",
        "Caixa de Entrada",
        "Inkomend",
        "Inkorg",
        "Indbakke",
        "Przychodzące",
        "Входящие",
        "受信",
        "受信トレイ",
        "收件箱",
        "收件匣",
        "받은 편지함",
        "Saapuneet",
        "Innboks",
        "Gelen Kutusu",
    ),
    "sent": (
        "Sent",
        "Sent Messages",
        "Sent Items",
        "Sent Mail",
        "Gesendet",
        "Envoyés",
        "Messages envoyés",
        "Enviado",
        "Enviadas",
        "Inviata",
        "Verstuurd",
        "Skickat",
        "Sendt",
        "Wysłane",
        "Отправленные",
        "送信済み",
        "已发出邮件",
        "已发送",
        "已傳送",
        "보낸 편지함",
        "보낸",
        "Lähetetyt",
        "Sendt",
        "Gönderilen",
    ),
    "drafts": (
        "Drafts",
        "Draft",
        "Entwürfe",
        "Brouillons",
        "Borradores",
        "Bozze",
        "Rascunhos",
        "Concepten",
        "Utkast",
        "Udkast",
        "Robocze",
        "Черновики",
        "下書き",
        "草稿",
        "임시 저장",
        "Luonnokset",
        "Taslaklar",
    ),
    "trash": (
        "Trash",
        "Deleted Items",
        "Deleted Messages",
        "Bin",
        "Papierkorb",
        "Corbeille",
        "Papelera",
        "Cestino",
        "Lixo",
        "Prullenmand",
        "Papperskorg",
        "Papirkurv",
        "Kosz",
        "Корзина",
        "ゴミ箱",
        "废纸篓",
        "垃圾桶",
        "휴지통",
        "Roskakori",
        "Papirkurv",
        "Çöp Sepeti",
    ),
    "junk": (
        "Junk",
        "Junk E-mail",
        "Junk Email",
        "Spam",
        "Bulk Mail",
        "Indésirable",
        "Indésirables",
        "No deseado",
        "Correo no deseado",
        "Indesiderata",
        "Indesejadas",
        "Skräp",
        "Reklamepost",
        "Niechciane",
        "Спам",
        "迷惑",
        "迷惑メール",
        "垃圾",
        "垃圾邮件",
        "垃圾郵件",
        "정크",
        "Roskapostit",
        "Uønsket",
        "İstenmeyen",
    ),
    "archive": (
        "Archive",
        "All Mail",
        "Archived",
        "Archiv",
        "Archives",
        "Archivo",
        "Archivio",
        "Arquivadas",
        "Archief",
        "Arkiv",
        "Archiwum",
        "Архив",
        "アーカイブ",
        "归档",
        "封存",
        "아카이브",
        "Arkisto",
        "Arkiv",
        "Arşiv",
    ),
}

_PROVIDER_PREFIX = re.compile(r"^\[[^\]]*\][/.]?")  # "[Gmail]/…"
_INBOX_PREFIX = re.compile(r"^inbox[/.]", re.IGNORECASE)  # "INBOX.Sent"
_SEPARATOR = re.compile(r"[/.]")


def normalize_mailbox_name(name: str | None) -> str:
    """Lowercased last segment, provider hierarchy dropped.

    Mirrors `MailCore.normalizeMailboxName`: "[Gmail]/Sent Mail" and
    dovecot's "INBOX.Sent" reduce to "sent mail" and "sent".
    """
    n = str(name if name is not None else "").strip().lower()
    n = _PROVIDER_PREFIX.sub("", n, count=1)
    n = _INBOX_PREFIX.sub("", n, count=1)
    parts = _SEPARATOR.split(n)
    return (parts[-1] or n).strip()


def is_top_level_mailbox(name: str | None) -> bool:
    """True for a mailbox Mail places at the top of an account.

    Mirrors `MailCore.isTopLevelMailbox`. Only PROVIDER prefixes are
    hierarchy we may ignore; "Projects/INBOX" is somebody's own folder
    and must never answer a request for the inbox.
    """
    n = str(name if name is not None else "").strip()
    n = _PROVIDER_PREFIX.sub("", n, count=1)
    n = _INBOX_PREFIX.sub("", n, count=1)
    return _SEPARATOR.search(n) is None


def mailbox_role(name: str | None) -> str | None:
    """The well-known role a mailbox name denotes, or None.

    Mirrors `MailCore.mailboxRole`.
    """
    n = normalize_mailbox_name(name)
    if not n:
        return None
    for role, aliases in MAILBOX_ROLES.items():
        for alias in aliases:
            if normalize_mailbox_name(alias) == n:
                return role
    return None
