from __future__ import annotations

import json
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .engine import Engine
from .navigation import NavigationContent
from .screens import ScreenCatalog
from .faq import FAQCatalog
from .mailer import MailTemplate, MailWorker
from .settings import InstanceLock, ROOT, Settings, TOKEN_PATTERN, load_secret
from .store import Store
from .telegram import APIError, TelegramAPI


class Delivery:
    def __init__(self, store: Store, api, emit):
        self.store, self.api, self.emit = store, api, emit
        self.sent_at: OrderedDict[int, float] = OrderedDict()
        with self.store.db:
            self.store.db.execute("UPDATE specialist_replies SET status='unknown',error='Перервано під час відправки. Перевірте доставку.' WHERE status='sending'")

    def render_menu(self, payload: dict, *, fresh_menu=False):
        chat_id = payload["chat_id"]
        menu = self.store.menu_message(chat_id)
        if menu and not fresh_menu:
            message_id = menu["message_id"]
            try:
                self.api.call("editMessageText", {**payload, "message_id": message_id})
            except APIError as error:
                if error.edit_unchanged:
                    return {"message_id": message_id, "date": menu["created_at"]}
                if not error.edit_unavailable:
                    raise
            else:
                return {"message_id": message_id, "date": menu["created_at"]}
        return self.api.call("sendMessage", payload)

    def flush(self):
        for job in self.store.ready_jobs(time.time()):
            destination = job["destination"]
            if time.monotonic() - self.sent_at.get(destination, -100) < 1.1:
                continue
            payload = json.loads(job["payload"])
            fresh_menu = payload.pop('_fresh_menu', False)
            payload.pop('_content_revision',None)
            payload.pop('_presentation_revision', None)
            reply_id = payload.pop('_reply_id', None)
            if reply_id:
                reply = self.store.db.execute('SELECT * FROM specialist_replies WHERE id=?', (reply_id,)).fetchone()
                if not reply or reply['status'] != 'pending':
                    with self.store.db:
                        self.store.complete(job['id'])
                    continue
                # Commit before HTTP: after a crash this must require manual reconciliation.
                with self.store.db:
                    self.store.db.execute("UPDATE specialist_replies SET status='sending' WHERE id=?", (reply_id,))
            old_menu = self.store.menu_message(destination) if job["is_menu"] else None
            try:
                result = self.render_menu(payload, fresh_menu=fresh_menu) if job["is_menu"] else self.api.call(job["method"], payload)
            except APIError as error:
                if reply_id:
                    state = 'failed' if error.code in (400, 403, 429) else 'unknown'
                    with self.store.db:
                        self.store.db.execute('UPDATE specialist_replies SET status=?,error=? WHERE id=?',
                                              (state, 'Відправка не вдалася.' if state == 'failed' else
                                               'Результат невідомий. Перевірте доставку перед повтором.', reply_id))
                        self.store.complete(job['id'])
                        self.store.enqueue('sendMessage', {'chat_id': reply['operator_id'],
                            'text': f"Звернення №{reply['ticket_id']}: " + ('Відправка не вдалася. Текст збережено.' if state == 'failed' else
                            'Результат відправки невідомий. Автоматичного повтору не буде. Текст збережено.')}, reply['ticket_id'])
                    continue
                if error.code in (401, 409):
                    raise
                with self.store.db:
                    if error.code in (0, 429) or error.code >= 500:
                        self.store.retry(job["id"], max(error.retry_after, min(60, 2 ** min(job["attempts"] + 1, 6))))
                        self.emit("log", f"Запит залишено в черзі: {error}")
                    else:
                        self.store.complete(job["id"])
                        if job["method"] == "deleteMessages":
                            self.emit("log", "Telegram не дозволив видалити частину повідомлень. Звернення закрите локально.")
                        elif job["method"] == "deleteMessage":
                            self.emit("log", "Telegram не дозволив приховати введену команду.")
                        elif job["method"] != "answerCallbackQuery":
                            ticket = f" для звернення №{job['ticket_id']}" if job["ticket_id"] else ""
                            self.emit("log", f"Не вдалося доставити повідомлення{ticket} у чат {destination}: {error}")
                continue
            with self.store.db:
                if reply_id:
                    self.store.db.execute("UPDATE specialist_replies SET status='sent',error='' WHERE id=?", (reply_id,))
                    self.store.add_message(reply['ticket_id'], 'operator', reply['body'])
                    self.store.db.execute("UPDATE specialist_chats SET draft='' WHERE ticket_id=? AND draft=?",
                                          (reply['ticket_id'], reply['body']))
                    self.store.enqueue('sendMessage', {'chat_id': reply['operator_id'], 'text':
                        'Повідомлення надіслано клієнту. Подальші відповіді клієнта надходитимуть у цей чат звернення.'}, reply['ticket_id'])
                if job["is_menu"]:
                    self.store.record_menu_message(destination, result["message_id"],
                                                   result.get("date", time.time()), job["ticket_id"])
                elif job["ticket_id"] and job["method"] == "sendMessage":
                    self.store.track(job["ticket_id"], destination, result["message_id"], result.get("date"))
                elif job["method"] == "deleteMessages":
                    menu = self.store.menu_message(destination)
                    if menu and menu["message_id"] in payload["message_ids"]:
                        self.store.forget_menu_message(destination, menu["message_id"])
                self.store.complete(job["id"])
            if old_menu and old_menu["message_id"] != result["message_id"]:
                try:
                    self.api.call("editMessageReplyMarkup", {
                        "chat_id": destination, "message_id": old_menu["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    })
                except APIError:
                    # The replacement is already saved; cleanup must not retry its delivery.
                    pass
            self.sent_at[destination] = time.monotonic()
            self.sent_at.move_to_end(destination)
            while len(self.sent_at) > 10000:
                self.sent_at.popitem(last=False)


class Runner:
    def __init__(self, token: str, settings: Settings, emit, root: Path = ROOT, faq_catalog: FAQCatalog | None = None):
        self.token, self.settings, self.emit, self.root = token, settings, emit, root
        self.faq_catalog = faq_catalog
        self.stop = threading.Event()
        self.controls: queue.Queue = queue.Queue()
        self.mail_worker = None
        self.thread = threading.Thread(target=self.run, name="InfoBot", daemon=False)

    def start(self):
        self.thread.start()

    def operator_changed(self, user_id: int):
        self.settings.save(self.root / "data" / "config.json")
        self.emit("operator", user_id)

    def run(self):
        store = None
        try:
            if not TOKEN_PATTERN.fullmatch(self.token):
                raise ValueError("Вставте повний токен BotFather")
            self.faq_catalog = self.faq_catalog or FAQCatalog.open(self.root)
            faq, _ = self.faq_catalog.snapshot()
            mail_template = MailTemplate.load(self.root / "mail_template.json")
            self.settings.mail.validate()
            mail_password = load_secret(self.root / "data" / "smtp.secret", "INFOBOT_SMTP_PASSWORD") if self.settings.mail.enabled else ""
            if self.settings.mail.enabled and not mail_password:
                raise ValueError("Укажіть пароль застосунку для пошти або вимкніть поштову пересилку до її налаштування")
            with InstanceLock(self.root / "data" / "run.lock"):
                api = TelegramAPI(self.token)
                identity = api.call("getMe")
                if api.call("getWebhookInfo").get("url"):
                    raise RuntimeError("У цього бота налаштовано webhook. Для окремого тесту створіть іншого бота в BotFather")
                database_path = self.root / "data" / f"bot_{identity['id']}.sqlite3"
                store = Store(database_path)
                engine = Engine(store, self.settings, faq, self.operator_changed, mail_template, identity.get("username", ""),
                                self.faq_catalog, NavigationContent(self.root / "content" / "navigation.json"),
                                ScreenCatalog(self.root / 'content' / 'screens.json'))
                delivery = Delivery(store, api, self.emit)
                if self.settings.mail.enabled:
                    self.mail_worker = MailWorker(database_path, self.settings.mail, mail_password, self.emit)
                    self.mail_worker.start()
                    self.emit("log", "Автоматичне пересилання звернень на електронну пошту ввімкнено")
                else:
                    self.emit("log", "Поштову пересилку вимкнено. Її можна налаштувати на вкладці «Електронна пошта»")
                mail_password = ""
                with store.db:
                    # A retry from a previous run must not publish an outdated menu.
                    store.db.execute("DELETE FROM outbox WHERE method='setMyCommands'")
                    store.enqueue("setMyCommands", {"commands": [
                        {"command": "start", "description": "Головне меню"},
                        {"command": "faq", "description": "Поширені запитання"},
                        {"command": "support", "description": "Підтримка"},
                        {"command": "ask", "description": "Запитання спеціалісту"},
                        {"command": "mine", "description": "Моє звернення"},
                        {"command": "continue", "description": "Продовжити анкету"},
                        {"command": "privacy", "description": "Про дані"},
                        {"command": "cancel", "description": "Скасувати введення"},
                        {"command": "back", "description": "Попередній крок анкети"},
                    ]})
                self.emit("running", identity.get("username", ""))
                self.emit("log", "Бота підключено до Telegram. Тексти повідомлень, токен і поштовий пароль у журнал не записуються.")
                if not self.settings.operator_id:
                    self.emit("pairing", engine.pairing.issue())
                failures = 0
                while not self.stop.is_set():
                    self.handle_controls(engine, store)
                    try:
                        delivery.flush()
                        self.emit("connections", store.connections())
                        self.emit('chats', store.chat_snapshot())
                        self.emit("counts", store.counts())
                        self.emit("mail_counts", store.mail_counts())
                        timeout = 1 if store.counts()[1] else 8
                        updates = api.call("getUpdates", {"offset": store.offset, "limit": 50, "timeout": timeout,
                                                           "allowed_updates": ["message", "callback_query"]})
                        if failures:
                            self.emit("log", "З’єднання з Telegram відновлено.")
                        failures = 0
                        for update in updates:
                            if self.stop.is_set():
                                break
                            engine.process(update)
                    except APIError as error:
                        if error.code in (400, 401, 403, 409):
                            raise
                        failures += 1
                        self.emit("log", str(error))
                        self.stop.wait(max(error.retry_after, min(30, 2 ** min(failures, 5))))
        except (ValueError, RuntimeError, APIError) as error:
            self.emit("error", str(error))
        except Exception as error:
            # Exception messages from third parties may contain request URLs or user text.
            self.emit("error", f"Не вдалося продовжити роботу ({type(error).__name__}). Перевірте доступ до папки, каталог content, faq.json та mail_template.json")
        finally:
            if self.mail_worker:
                self.mail_worker.stop.set()
                self.mail_worker.thread.join()
            if store:
                if 'engine' in locals():
                    self.handle_controls(engine, store)
                store.close()
            self.token = ""
            self.emit("stopped", None)

    def handle_controls(self, engine: Engine, store: Store):
        while not self.controls.empty():
            action = self.controls.get_nowait()
            if isinstance(action, dict):
                try:
                    self.chat_control(engine, store, action)
                except (ValueError, RuntimeError) as error:
                    self.emit('chat_error', str(error))
                self.emit('chat_ack', action['ticket_id'])
                self.emit('chats', store.chat_snapshot())
                continue
            if action == "pairing":
                self.emit("pairing", engine.pairing.issue())
            elif action == "backup":
                path = store.backup(self.root / "data" / "backups")
                faq_path = path.with_suffix('.faq.json')
                try:
                    engine.faq_catalog.backup(faq_path)
                except (OSError, ValueError):
                    self.emit('log', 'Копію бази створено, але FAQ скопіювати не вдалося. Перевірте папку резервних копій.')
                    self.emit('backup', str(path) + '\nFAQ не скопійовано.')
                else:
                    self.emit("backup", str(path) + '\n' + str(faq_path))
            elif action == "retry_mail" and self.mail_worker:
                if self.mail_worker.thread.is_alive():
                    self.mail_worker.retry_requested.set()
                    self.emit("log", "Заплановано повторне надсилання листів із черги")
                else:
                    self.emit("log", "Для відновлення поштового потоку перевірте налаштування та перезапустіть бота")

    def chat_control(self, engine, store, action):
        if not self.settings.operator_id or action.get('operator_id') != self.settings.operator_id:
            raise ValueError('Доступ лише для підключеного спеціаліста.')
        ticket_id = action['ticket_id']
        ticket = store.ticket(ticket_id)
        if not ticket:
            raise ValueError('Звернення вже завершено.')
        with store.db:
            kind = action['action']
            if kind == 'draft':
                store.save_chat_draft(ticket_id, action['text'])
            elif kind == 'read':
                store.read_chat(ticket_id, action.get('read_id'))
            elif kind == 'send':
                engine.reply(self.settings.operator_id, ticket_id, action['text'], {})
            elif kind == 'retry':
                row = store.db.execute('SELECT * FROM specialist_replies WHERE id=? AND ticket_id=?',
                                       (action['reply_id'], ticket_id)).fetchone()
                if row and row['status'] in ('failed', 'unknown'):
                    store.db.execute("UPDATE specialist_replies SET status='cancelled' WHERE id=?", (row['id'],))
                    engine.reply(self.settings.operator_id, ticket_id, row['body'], {})
            elif kind == 'close':
                engine.close_ticket(ticket)
