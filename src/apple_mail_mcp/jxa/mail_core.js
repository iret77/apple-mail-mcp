/**
 * Apple Mail JXA Core Library
 *
 * Shared utilities for fast, batch-optimized Mail.app automation.
 * This library is injected into all JXA scripts to provide consistent
 * error handling, account/mailbox resolution, and batch fetching.
 */

const Mail = Application("Mail");

const MailCore = {
    /**
     * Get an account by name, or the first account if name is null/empty.
     * @param {string|null} name - Account name or null for default
     * @returns {Account} Mail account object
     */
    getAccount(name) {
        if (name) {
            return Mail.accounts.byName(name);
        }
        const accounts = Mail.accounts();
        if (accounts.length === 0) {
            throw new Error("No mail accounts configured");
        }
        return accounts[0];
    },
    /**
     * Role → the names Mail is known to use for it.
     *
     * A name is the WEAKEST way to find a well-known mailbox: it changes
     * with the system language, with the macOS version, and with the
     * provider. This table is therefore the last resort, not the first
     * — see getMailbox below. Every entry is taken from Apple's own
     * localized Mail user guide or is a documented provider/legacy
     * name; nothing here is a translation someone made up.
     */
    MAILBOX_ROLES: {
        inbox: [
            "INBOX", "Inbox", "In",
            "Eingang", "Posteingang",              // de (docs / observed)
            "Boîte de réception",                  // fr
            "Entrada",                             // es
            "Entrata",                             // it
            "Caixa de Entrada",                    // pt-BR
            "Inkomend",                            // nl
            "Inkorg",                              // sv
            "Indbakke",                            // da
            "Przychodzące",                        // pl
            "Входящие",                            // ru
            "受信", "受信トレイ",                    // ja
            "收件箱",                              // zh-Hans
            "收件匣",                              // zh-Hant
            "받은 편지함",                          // ko
            "Saapuneet",                           // fi
            "Innboks",                             // no
            "Gelen Kutusu",                        // tr
        ],
        sent: [
            "Sent", "Sent Messages", "Sent Items", "Sent Mail", "Out",
            "Gesendet",
            "Envoyés", "Messages envoyés",
            "Enviado", "Enviadas",
            "Inviata",
            "Verstuurd",
            "Skickat", "Sendt",
            "Wysłane",
            "Отправленные",
            "送信済み",
            "已发出邮件", "发件箱", "已傳送",
            "보낸 편지함", "보낸",
            "Lähetetyt", "Sendt", "Gönderilen",
        ],
        drafts: [
            "Drafts", "Draft",
            "Entwürfe", "Brouillons", "Borradores", "Bozze",
            "Rascunhos", "Concepten", "Utkast", "Udkast", "Robocze",
            "Черновики", "下書き", "草稿",
            "임시 저장", "Luonnokset", "Taslaklar",
        ],
        trash: [
            "Trash", "Deleted Items", "Deleted Messages", "Bin",
            "Papierkorb", "Corbeille", "Papelera", "Cestino", "Lixo",
            "Prullenmand", "Papperskorg", "Papirkurv", "Kosz",
            "Корзина", "ゴミ箱", "废纸篓", "垃圾桶",
            "휴지통", "Roskakori", "Papirkurv", "Çöp Sepeti",
        ],
        junk: [
            "Junk", "Junk E-mail", "Junk Email", "Spam", "Bulk Mail",
            "Indésirable", "Indésirables",
            "No deseado", "Correo no deseado",
            "Indesiderata", "Indesejadas",
            "Skräp", "Reklamepost", "Niechciane",
            "Спам", "迷惑", "迷惑メール", "垃圾", "垃圾邮件",
            "垃圾郵件", "정크", "Roskapostit", "Uønsket",
            "İstenmeyen",
        ],
        archive: [
            "Archive", "All Mail", "Archived",
            "Archiv", "Archives", "Archivo", "Archivio", "Arquivadas",
            "Archief", "Arkiv", "Archiwum",
            "Архив", "アーカイブ", "归档", "封存",
            "아카이브", "Arkisto", "Arkiv", "Arşiv",
        ],
    },

    /**
     * Strip everything that is decoration rather than identity.
     *
     * Providers wrap the same mailbox in their own hierarchy —
     * "[Gmail]/Sent Mail", "INBOX.Sent" on dovecot, "INBOX/Trash" —
     * and case varies freely. Comparing the bare last segment makes
     * those all resolve.
     */
    normalizeMailboxName(name) {
        let n = String(name == null ? "" : name).trim().toLowerCase();
        n = n.replace(/^\[[^\]]*\][\/.]?/, "");   // "[Gmail]/…"
        n = n.replace(/^inbox[\/.]/, "");          // "INBOX.Sent"
        const parts = n.split(/[\/.]/);
        return (parts[parts.length - 1] || n).trim();
    },

    /** Which well-known role does this name denote, if any? */
    mailboxRole(name) {
        const n = this.normalizeMailboxName(name);
        if (!n) return null;
        for (const role of Object.keys(this.MAILBOX_ROLES)) {
            for (const alias of this.MAILBOX_ROLES[role]) {
                if (this.normalizeMailboxName(alias) === n) return role;
            }
        }
        return null;
    },

    /**
     * Trash or junk — a mailbox a recovered write must not land in.
     * Role-based, so it holds in every language and on every provider.
     */
    isDiscardMailbox(name) {
        const role = this.mailboxRole(name);
        return role === "trash" || role === "junk";
    },

    /**
     * Ask Mail itself which mailbox fills a role for this account.
     *
     * Language- and provider-independent when it works. The property
     * is not guaranteed to exist on every macOS version, so this is a
     * probe: it either returns a mailbox or null, and never throws.
     */
    specialMailbox(account, role) {
        const props = {
            sent: "sentMailbox",
            drafts: "draftsMailbox",
            trash: "trashMailbox",
            junk: "junkMailbox",
        };
        const prop = props[role];
        if (!prop) return null;
        for (const owner of [account, Mail]) {
            try {
                const mb = owner[prop]();
                if (mb && mb.name()) return mb;
            } catch (e) {
                // property absent or not applicable — try the next
            }
        }
        return null;
    },

    /**
     * Resolve a mailbox by name, from most reliable to least.
     *
     * The order matters: a name is what breaks first. A German Mail
     * has no "INBOX" but a "Posteingang"; Exchange says "Deleted
     * Items"; Gmail hides its folders under "[Gmail]/". So the exact
     * name is tried first because it is cheap, then Mail's own notion
     * of the role, then normalized matching that ignores hierarchy and
     * case, and only then the name table.
     */
    getMailbox(account, name) {
        // 1. Exact match — cheap and correct when it hits.
        try {
            const mb = account.mailboxes.byName(name);
            mb.name();  // force evaluation to detect -1728 early
            return mb;
        } catch (_) {
            // fall through
        }

        const role = this.mailboxRole(name);
        let names = [];
        try {
            names = account.mailboxes.name();
        } catch (e) {
            names = [];
        }

        // 2. Ask Mail which mailbox plays this role for the account.
        if (role) {
            const special = this.specialMailbox(account, role);
            if (special) return special;
        }

        // 3. Same role, different word — the localized/legacy table.
        //    This runs BEFORE the generic normalized match, and the
        //    order is load-bearing: normalization drops provider
        //    hierarchy, so a user's own "Projects/INBOX" normalizes to
        //    "inbox" and would answer a request for the real inbox. On
        //    a German account (Posteingang + Projects/INBOX) that
        //    returned the subfolder — the wrong mailbox, silently.
        //    A ROLE request is answered by a mailbox that plays the
        //    role; only a non-role name falls through to shape matching.
        if (role) {
            for (const actual of names) {
                if (this.mailboxRole(actual) === role) {
                    return account.mailboxes.byName(actual);
                }
            }
        }

        // 4. Normalized match: ignores case and provider hierarchy, so
        //    "[Gmail]/Sent Mail" answers a request for "Sent Mail" and
        //    "INBOX.Projects" answers "Projects".
        const wanted = this.normalizeMailboxName(name);
        for (const actual of names) {
            if (this.normalizeMailboxName(actual) === wanted) {
                return account.mailboxes.byName(actual);
            }
        }

        // 5. Nothing matched. Name what IS there: the caller can only
        //    act on this if it learns which mailboxes exist.
        throw new Error(
            "No mailbox matching " + JSON.stringify(String(name)) +
            (role ? " (role: " + role + ")" : "") +
            ". Available: " + names.join(", ")
        );
    },

    /**
     * Batch fetch multiple properties from a messages collection.
     * This is THE critical optimization - one IPC call per property
     * instead of one per message.
     *
     * @param {Messages} msgs - Messages collection from a mailbox
     * @param {string[]} props - Property names to fetch
     * @returns {Object} Map of property name to array of values
     */
    batchFetch(msgs, props) {
        const result = {};
        for (const prop of props) {
            result[prop] = msgs[prop]();
        }
        return result;
    },

    /**
     * Get message IDs for referencing specific messages later.
     * @param {Messages} msgs - Messages collection
     * @returns {string[]} Array of message IDs
     */
    getMessageIds(msgs) {
        return msgs.id();
    },

    /**
     * Get a specific message by ID.
     * @param {string} messageId - The message ID
     * @returns {Message} Message object
     */
    getMessageById(messageId) {
        // Messages are referenced by ID across all accounts
        return Mail.messages.byId(messageId);
    },

    /**
     * Wrap an operation with error handling.
     * @param {Function} fn - Function to execute
     * @returns {Object} {ok: true, data: ...} or {ok: false, error: ...}
     */
    safely(fn) {
        try {
            return { ok: true, data: fn() };
        } catch (e) {
            return { ok: false, error: String(e) };
        }
    },

    /**
     * Get today's date at midnight for filtering.
     * @returns {Date} Today at 00:00:00
     */
    today() {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        return d;
    },

    /**
     * Get a date N days ago at midnight for filtering.
     * @param {number} days - Number of days ago
     * @returns {Date} Date at 00:00:00 N days ago
     */
    daysAgo(days) {
        const d = new Date();
        d.setDate(d.getDate() - days);
        d.setHours(0, 0, 0, 0);
        return d;
    },

    /**
     * Format a date for JSON output.
     * @param {Date} date - Date to format
     * @returns {string} ISO string or null if invalid
     */
    formatDate(date) {
        if (!date || !(date instanceof Date)) return null;
        return date.toISOString();
    },

    /**
     * List all accounts.
     * @returns {Object[]} Array of {name, id} objects
     */
    listAccounts() {
        const accounts = Mail.accounts();
        const names = Mail.accounts.name();
        const ids = Mail.accounts.id();
        const results = [];
        for (let i = 0; i < accounts.length; i++) {
            results.push({ name: names[i], id: ids[i] });
        }
        return results;
    },

    /**
     * List mailboxes for an account.
     * Note: messageCount is not available via batch fetch, only unreadCount.
     * @param {Account} account - Mail account
     * @returns {Object[]} Array of {name, unreadCount}
     */
    listMailboxes(account) {
        const mboxes = account.mailboxes();
        const names = account.mailboxes.name();
        const unread = account.mailboxes.unreadCount();
        const results = [];
        for (let i = 0; i < mboxes.length; i++) {
            results.push({
                name: names[i],
                unreadCount: unread[i],
            });
        }
        return results;
    },
};
