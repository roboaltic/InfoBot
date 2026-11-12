from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS client_navigation (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT NOT NULL,
                connected_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                client_name TEXT NOT NULL DEFAULT '',
                client_username TEXT NOT NULL DEFAULT '',
                mail_thread_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_refs (
                ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS menu_messages (
                chat_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                payload TEXT NOT NULL,
                destination INTEGER NOT NULL,
                ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                is_menu INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS messages_ticket ON messages(ticket_id, id);
            CREATE INDEX IF NOT EXISTS outbox_destination ON outbox(destination, id);
            CREATE TABLE IF NOT EXISTS mail_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                message_row_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','blocked')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS mail_status ON mail_outbox(status, next_attempt, id);
            CREATE TABLE IF NOT EXISTS drafts (
                user_id INTEGER PRIMARY KEY,
                ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                telegram_name TEXT NOT NULL DEFAULT '',
                telegram_username TEXT NOT NULL DEFAULT '',
                step TEXT NOT NULL DEFAULT 'email',
                client_email TEXT NOT NULL DEFAULT '',
                client_phone TEXT NOT NULL DEFAULT '',
                client_full_name TEXT NOT NULL DEFAULT '',
                client_first_name TEXT NOT NULL DEFAULT '',
                client_company TEXT NOT NULL DEFAULT '',
                message_refs TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        # Additive migration: preserve every existing 0.1 database and its queue.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tickets)")}
        with self.db:
            for name in ("client_name", "client_username", "mail_thread_id", "client_email", "client_phone",
                         "client_full_name", "client_first_name", "client_company"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE tickets ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            outbox_columns = {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}
            if 'draft_user_id' not in outbox_columns:
                self.db.execute('ALTER TABLE outbox ADD COLUMN draft_user_id INTEGER REFERENCES drafts(user_id) ON DELETE CASCADE')
            if 'is_menu' not in outbox_columns:
                self.db.execute('ALTER TABLE outbox ADD COLUMN is_menu INTEGER NOT NULL DEFAULT 0')
            for table in ('drafts', 'tickets'):
                existing = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
                if 'grant_context' not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN grant_context TEXT NOT NULL DEFAULT '{{}}'")
            if 'form_version' not in {row[1] for row in self.db.execute('PRAGMA table_info(drafts)')}:
                self.db.execute('ALTER TABLE drafts ADD COLUMN form_version INTEGER NOT NULL DEFAULT 1')
            draft_columns = {row[1] for row in self.db.execute('PRAGMA table_info(drafts)')}
            for name in ('editing_field', 'submission_id'):
                if name not in draft_columns:
                    self.db.execute(f"ALTER TABLE drafts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            ticket_columns = {row[1] for row in self.db.execute('PRAGMA table_info(tickets)')}
            for name, declaration in (('submission_id', "TEXT NOT NULL DEFAULT ''"),
                                      ('accepted_by', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('accepted_at', 'REAL NOT NULL DEFAULT 0')):
                if name not in ticket_columns:
                    self.db.execute(f'ALTER TABLE tickets ADD COLUMN {name} {declaration}')
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tickets_submission ON tickets(submission_id) WHERE submission_id<>''")
            self.db.execute('CREATE TABLE IF NOT EXISTS static_menu_history (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))')
            self.db.execute('INSERT OR IGNORE INTO static_menu_history SELECT chat_id,message_id FROM menu_messages')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS specialist_chats (
                    ticket_id INTEGER PRIMARY KEY REFERENCES tickets(id) ON DELETE CASCADE,
                    draft TEXT NOT NULL DEFAULT '', read_id INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS specialist_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    operator_id INTEGER NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '');
            ''')

    def save_chat_draft(self, ticket_id, text):
        self.db.execute('INSERT INTO specialist_chats(ticket_id,draft) VALUES (?,?) '
                        'ON CONFLICT(ticket_id) DO UPDATE SET draft=excluded.draft', (ticket_id, text))

    def read_chat(self, ticket_id, read_id=None):
        self.db.execute('INSERT OR IGNORE INTO specialist_chats(ticket_id) VALUES (?)', (ticket_id,))
        if read_id is None:
            read_id = self.db.execute('SELECT COALESCE(max(id),0) FROM messages WHERE ticket_id=?',
                                      (ticket_id,)).fetchone()[0]
        self.db.execute('UPDATE specialist_chats SET read_id=max(read_id,?) WHERE ticket_id=?', (read_id, ticket_id))

    def chat_snapshot(self):
        rows = self.db.execute('''SELECT t.*, COALESCE(c.draft,'') AS draft,
            (SELECT count(*) FROM messages m WHERE m.ticket_id=t.id AND m.direction='client'
             AND m.id>COALESCE(c.read_id,0)) AS unread
            FROM tickets t LEFT JOIN specialist_chats c ON c.ticket_id=t.id ORDER BY t.id DESC''')
        return [{**dict(row), 'history': [dict(m) for m in self.history(row['id'], -1)],
                 'replies': [dict(r) for r in self.db.execute(
                     'SELECT * FROM specialist_replies WHERE ticket_id=? ORDER BY id', (row['id'],))]}
                for row in rows]

    def queue_reply(self, ticket_id, operator_id, text):
        if self.db.execute("SELECT 1 FROM specialist_replies WHERE ticket_id=? AND status IN ('pending','sending','unknown')",
                           (ticket_id,)).fetchone():
            return False
        ticket = self.ticket(ticket_id)
        if not ticket:
            return False
        self.save_chat_draft(ticket_id, text)
        cursor = self.db.execute('INSERT INTO specialist_replies(ticket_id,operator_id,body) VALUES (?,?,?)',
                                 (ticket_id, operator_id, text))
        self.enqueue('sendMessage', {'chat_id': ticket['user_id'],
                     'text': f'Відповідь спеціаліста · звернення №{ticket_id}\n\n{text}',
                     '_reply_id': cursor.lastrowid}, ticket_id)
        return True

    def navigation(self, user_id: int) -> dict:
        row = self.db.execute('SELECT state FROM client_navigation WHERE user_id=?', (user_id,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save_navigation(self, user_id: int, state: dict):
        self.db.execute('INSERT INTO client_navigation VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state',
                        (user_id, json.dumps(state, ensure_ascii=False)))

    def close(self):
        self.db.close()

    @property
    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(offset),))

    def record_connection(self, user_id: int, actor: dict):
        name = ' '.join(str(actor.get(key) or '') for key in ('first_name', 'last_name')).strip()
        self.db.execute("""INSERT INTO connections VALUES (?,?,?,?)
                           ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username""",
                        (user_id, name, actor.get('username') or '', time.time()))

    def connections(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM connections ORDER BY connected_at DESC, user_id')]

    def ticket(self, ticket_id: int):
        return self.db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def user_ticket(self, user_id: int):
        return self.db.execute("SELECT * FROM tickets WHERE user_id=?", (user_id,)).fetchone()

    def create_ticket(self, user_id: int, client_name: str = "", client_username: str = "", *, submission_id: str = '') -> int:
        cursor = self.db.execute("INSERT INTO tickets(user_id,created_at,client_name,client_username,submission_id) VALUES (?,?,?,?,?)",
                                 (user_id, time.time(), client_name, client_username, submission_id))
        return cursor.lastrowid

    def draft(self, user_id: int):
        return self.db.execute('SELECT * FROM drafts WHERE user_id=?', (user_id,)).fetchone()

    def create_draft(self, user_id: int, question: str, telegram_name: str, telegram_username: str,
                     message_id: int, date: float, ticket_id: int | None = None):
        self.db.execute('''INSERT INTO drafts(user_id,ticket_id,question,telegram_name,telegram_username,message_refs,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?)''',
                        (user_id, ticket_id, question, telegram_name, telegram_username,
                         json.dumps([{'id': message_id, 'date': date}] if message_id > 0 else []), time.time(), time.time()))

    def update_draft(self, user_id: int, **values):
        allowed = {'step', 'client_email', 'client_phone', 'client_full_name', 'client_first_name', 'client_company',
                   'question', 'grant_context', 'form_version', 'editing_field', 'submission_id'}
        if not values or not set(values) <= allowed:
            raise ValueError('Недопустимі поля чернетки')
        fields = ','.join(key + '=?' for key in values)
        self.db.execute('UPDATE drafts SET ' + fields + ',updated_at=? WHERE user_id=?', (*values.values(), time.time(), user_id))

    def track_draft(self, user_id: int, message_id: int, date: float):
        draft = self.draft(user_id)
        references = json.loads(draft['message_refs'])
        if not any(item['id'] == message_id for item in references):
            references.append({'id': message_id, 'date': date})
        self.db.execute('UPDATE drafts SET message_refs=? WHERE user_id=?', (json.dumps(references), user_id))

    def set_contacts(self, ticket_id: int, contacts):
        self.db.execute('''UPDATE tickets SET client_email=?,client_phone=?,client_full_name=?,client_first_name=?,client_company=? WHERE id=?''',
                        (*(contacts[key] for key in ('client_email', 'client_phone', 'client_full_name', 'client_first_name', 'client_company')), ticket_id))

    def delete_draft(self, user_id: int):
        self.db.execute('DELETE FROM drafts WHERE user_id=?', (user_id,))

    def cancel_draft(self, user_id: int, delete_telegram: bool):
        draft = self.draft(user_id)
        if draft and delete_telegram:
            identifiers = [item['id'] for item in json.loads(draft['message_refs']) if item['date'] > time.time() - 47.9 * 3600]
            for start in range(0, len(identifiers), 100):
                self.enqueue('deleteMessages', {'chat_id': user_id, 'message_ids': identifiers[start:start + 100]})
        self.delete_draft(user_id)

    def add_message(self, ticket_id: int, direction: str, body: str):
        cursor = self.db.execute("INSERT INTO messages(ticket_id,direction,body,created_at) VALUES (?,?,?,?)",
                                 (ticket_id, direction, body, time.time()))
        return cursor.lastrowid

    def message_count(self, ticket_id: int) -> int:
        return self.db.execute("SELECT count(*) FROM messages WHERE ticket_id=? AND direction='client'", (ticket_id,)).fetchone()[0]

    def enqueue_mail(self, ticket_id: int, message_row_id: int, payload: dict):
        self.db.execute("INSERT OR IGNORE INTO mail_outbox(ticket_id,message_row_id,payload) VALUES (?,?,?)",
                        (ticket_id, message_row_id, json.dumps(payload, ensure_ascii=False)))

    def ready_mail(self):
        return self.db.execute("""
            SELECT q.* FROM mail_outbox q WHERE status='pending' AND next_attempt<=?
            AND NOT EXISTS (SELECT 1 FROM mail_outbox older WHERE older.ticket_id=q.ticket_id
                AND older.id<q.id AND older.status IN ('pending','blocked')) ORDER BY id LIMIT 1
        """, (time.time(),)).fetchone()

    def mail_counts(self, ticket_id: int | None = None):
        condition = " WHERE ticket_id=?" if ticket_id is not None else ""
        params = (ticket_id,) if ticket_id is not None else ()
        counts = {row[0]: row[1] for row in self.db.execute(
            "SELECT status,count(*) FROM mail_outbox" + condition + " GROUP BY status", params)}
        return counts.get("pending", 0), counts.get("sent", 0), counts.get("blocked", 0)

    def retry_mail(self, mail_id: int, delay: float, error: str, blocked: bool = False):
        self.db.execute("UPDATE mail_outbox SET status=?,attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
                        ("blocked" if blocked else "pending", time.time() + delay, error, mail_id))

    def mark_mail_sent(self, mail_id: int):
        self.db.execute("UPDATE mail_outbox SET status='sent',payload='{}',last_error='' WHERE id=?", (mail_id,))

    def resume_mail(self):
        self.db.execute("UPDATE mail_outbox SET status='pending',next_attempt=0,last_error='' WHERE status IN ('blocked','pending')")

    def history(self, ticket_id: int, limit: int = 3):
        return list(reversed(self.db.execute(
            "SELECT * FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (ticket_id, limit)
        ).fetchall()))

    def tickets(self, page: int = 0, page_size: int = 8):
        return self.db.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT ? OFFSET ?",
                               (page_size, page * page_size)).fetchall()

    def counts(self):
        return (self.db.execute("SELECT count(*) FROM tickets").fetchone()[0],
                self.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def track(self, ticket_id: int, chat_id: int, message_id: int, date: float | None = None):
        if self.ticket(ticket_id):
            self.db.execute("INSERT OR IGNORE INTO telegram_refs VALUES (?,?,?,?)",
                            (ticket_id, chat_id, message_id, date if date is not None else time.time()))

    def menu_message(self, chat_id: int):
        return self.db.execute('SELECT * FROM menu_messages WHERE chat_id=?', (chat_id,)).fetchone()

    def record_menu_message(self, chat_id: int, message_id: int, created_at: float, ticket_id: int | None):
        self.db.execute('INSERT OR IGNORE INTO static_menu_history VALUES (?,?)', (chat_id, message_id))
        self.db.execute('INSERT OR REPLACE INTO menu_messages VALUES (?,?,?)', (chat_id, message_id, created_at))
        self.db.execute('DELETE FROM telegram_refs WHERE chat_id=? AND message_id=?', (chat_id, message_id))
        if ticket_id is not None:
            self.track(ticket_id, chat_id, message_id, created_at)

    def forget_menu_message(self, chat_id: int, message_id: int):
        self.db.execute('DELETE FROM menu_messages WHERE chat_id=? AND message_id=?', (chat_id, message_id))

    def was_menu_message(self, chat_id, message_id):
        return self.db.execute('SELECT 1 FROM static_menu_history WHERE chat_id=? AND message_id=?',
                               (chat_id, message_id)).fetchone() is not None

    def enqueue(self, method: str, payload: dict, ticket_id: int | None = None, draft_user_id: int | None = None,
                *, is_menu: bool = False):
        self.db.execute("INSERT INTO outbox(method,payload,destination,ticket_id,draft_user_id,is_menu) VALUES (?,?,?,?,?,?)",
                        (method, json.dumps(payload, ensure_ascii=False), payload.get("chat_id", 0), ticket_id, draft_user_id, is_menu))

    def ready_jobs(self, now: float):
        # Preserve message order within each chat, including while retrying.
        return self.db.execute("""
            SELECT * FROM outbox q WHERE next_attempt<=? AND NOT EXISTS (
                SELECT 1 FROM outbox older WHERE older.destination=q.destination AND older.id<q.id
            ) ORDER BY id LIMIT 20
        """, (now,)).fetchall()

    def complete(self, job_id: int):
        self.db.execute("DELETE FROM outbox WHERE id=?", (job_id,))

    def retry(self, job_id: int, delay: float):
        self.db.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?",
                        (time.time() + delay, job_id))

    def purge_ticket(self, ticket_id: int, delete_telegram: bool) -> list[int]:
        cutoff = time.time() - 47.9 * 3600
        references = self.db.execute('''
            SELECT r.* FROM telegram_refs r LEFT JOIN menu_messages m USING (chat_id, message_id)
            WHERE r.ticket_id=? AND (r.created_at>? OR m.message_id IS NOT NULL)
        ''', (ticket_id, cutoff)).fetchall() if delete_telegram else []
        self.db.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        by_chat: dict[int, list[int]] = {}
        menus_to_clear = []
        for row in references:
            if row["created_at"] > cutoff:
                by_chat.setdefault(row["chat_id"], []).append(row["message_id"])
            else:
                # Older menu messages can still be cleared by editing them.
                menus_to_clear.append(row["chat_id"])
        for chat_id, message_ids in by_chat.items():
            for start in range(0, len(message_ids), 100):
                self.enqueue("deleteMessages", {"chat_id": chat_id, "message_ids": message_ids[start:start + 100]})
        return menus_to_clear

    def backup(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f_UTC")
        prefix = "infobot" if self.path == ":memory:" else Path(self.path).stem
        path = directory / f"{prefix}_{stamp}.sqlite3"
        with closing(sqlite3.connect(path)) as target:
            self.db.backup(target)
        return path
