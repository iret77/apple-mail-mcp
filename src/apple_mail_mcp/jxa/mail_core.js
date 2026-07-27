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
     * Get a mailbox from an account.
     *
     * Tries an exact match first, then falls back to
     * case-insensitive matching and common aliases
     * (e.g. "Sent Messages" → "Sent Items").
     *
     * @param {Account} account - Mail account object
     * @param {string} name - Mailbox name (e.g., "INBOX", "Sent")
     * @returns {Mailbox} Mailbox object
     */
    getMailbox(account, name) {
        // Fast path: exact match
        try {
            const mb = account.mailboxes.byName(name);
            // Force evaluation to detect -1728 early
            mb.name();
            return mb;
        } catch (_) {
            // Fall through to fuzzy matching
        }

        // Alias groups: names that refer to the same logical
        // mailbox across different providers/locales
        // Mail.app shows these names in the system language, so a
        // German install has no "INBOX" at all — it has "Posteingang".
        // Without the localized names the default mailbox simply does
        // not resolve, and every JXA path fails with a bare -1728.
        const aliases = [
            [
                "INBOX", "Inbox",
                "Posteingang", "Boîte de réception", "Bandeja de entrada",
                "Posta in arrivo", "Caixa de entrada", "Postvak IN",
                "Inkorg", "Innboks", "Indbakke", "Saapuneet",
                "Skrzynka odbiorcza", "Doručená pošta", "Входящие",
            ],
            [
                "Sent", "Sent Items", "Sent Messages", "Sent Mail",
                "Gesendet", "Gesendete Objekte", "Gesendete E-Mails",
                "Envoyés", "Messages envoyés", "Enviados",
                "Posta inviata", "Verzonden", "Skickat", "Sendt",
                "Wysłane", "Отправленные",
            ],
            [
                "Trash", "Deleted Items", "Deleted Messages", "Bin",
                "Papierkorb", "Gelöschte Objekte", "Corbeille",
                "Papelera", "Cestino", "Lixeira", "Prullenmand",
                "Papperskorg", "Papirkurv", "Roskakori", "Kosz",
                "Корзина",
            ],
            [
                "Drafts", "Draft",
                "Entwürfe", "Brouillons", "Borradores", "Bozze",
                "Rascunhos", "Concepten", "Utkast", "Kladder",
                "Luonnokset", "Robocze", "Черновики",
            ],
            [
                "Junk", "Junk Email", "Junk E-mail", "Spam",
                "Werbung", "Unerwünschte Werbung", "Indésirables",
                "Correo no deseado", "Posta indesiderata", "Ongewenst",
                "Skräppost", "Søppelpost", "Roskaposti", "Niechciane",
                "Спам",
            ],
            [
                "Archive", "All Mail",
                "Archiv", "Archives", "Archivo", "Archivio",
                "Arquivo", "Archief", "Arkiv", "Arkisto", "Archiwum",
                "Архив",
            ],
        ];

        const lower = name.toLowerCase();
        const names = account.mailboxes.name();

        // Find which alias group the requested name belongs to
        let candidates = null;
        for (const group of aliases) {
            if (group.some((a) => a.toLowerCase() === lower)) {
                candidates = group;
                break;
            }
        }

        // Try alias group members first
        if (candidates) {
            for (const alt of candidates) {
                if (names.some((n) => n === alt)) {
                    return account.mailboxes.byName(alt);
                }
            }
        }

        // Last resort: case-insensitive match on actual names
        for (const actual of names) {
            if (actual.toLowerCase() === lower) {
                return account.mailboxes.byName(actual);
            }
        }

        // Nothing found — throw the standard error
        return account.mailboxes.byName(name);
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
        const failed = [];
        let len = -1;
        for (const prop of props) {
            try {
                result[prop] = msgs[prop]();
                if (len < 0 && result[prop]) len = result[prop].length;
            } catch (e) {
                // One property Mail refuses to report in bulk must not
                // take the whole listing down with it — pad it instead,
                // so the caller's per-index arithmetic still lines up.
                failed.push(prop);
            }
        }
        if (len < 0) {
            // Nothing came back at all: this is a real failure (bad
            // mailbox, no access) and must surface, not read as "0
            // messages".
            throw new Error(
                "batchFetch: no property could be read (" +
                props.join(", ") + ")"
            );
        }
        for (const prop of failed) {
            result[prop] = new Array(len).fill(null);
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
