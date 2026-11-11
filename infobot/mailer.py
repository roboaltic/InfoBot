from __future__ import annotations

import json
import smtplib
import ssl
import string
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from .settings import MailSettings, validate_address
from .store import Store

FIELDS = {"ticket_id", "message_number", "event_label", "accepted_at", "client_id",
          "client_name", "client_username", "client_link", "question", "bot_username",
          "client_email", "client_phone", "client_full_name", "client_first_name", "client_company"}
DEFAULT_TEMPLATE = {
    "subject": "[InfoBot] Звернення №{ticket_id}",
    "body": (
        "ЗВЕРНЕННЯ КЛІЄНТА №{ticket_id}\n"
        "Тип: {event_label}\nПовідомлення клієнта: №{message_number}\n"
        "Прийнято ботом: {accepted_at}\n\n"
        "ПІБ: {client_full_name}\nКомпанія: {client_company}\n"
        "Електронна пошта: {client_email}\nТелефон: {client_phone}\n"
        "Ім’я профілю Telegram: {client_name}\nІм’я користувача Telegram: {client_username}\n"
        "Telegram ID: {client_id}\nПосилання: {client_link}\n\n"
        "ТЕКСТ ЗАПИТАННЯ\n{question}\n\n"
        "Надіслано автоматично ботом {bot_username}.\n"
        "Якщо підключено диспетчера, відповідь клієнту можна надіслати в чаті з ботом:\n"
        "/reply {ticket_id} Текст відповіді\n\n"
        "Відповіді на цей електронний лист автоматично в Telegram не передаються."
    ),
}


class MailTemplate:
    def __init__(self, data: dict | None = None):
        self.data = DEFAULT_TEMPLATE.copy() if data is None else data
        if not isinstance(self.data, dict) or set(self.data) != {"subject", "body"}:
            raise ValueError("mail_template.json має містити лише поля subject та body")
        formatter = string.Formatter()
        for key, text in self.data.items():
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("Поля шаблону листа мають містити текст довжиною до 20000 знаків")
            try:
                fields = set()
                for literal, name, spec, conversion in formatter.parse(text):
                    if name is not None:
                        if name not in FIELDS or spec or conversion:
                            raise ValueError("Невідоме поле або формат у шаблоні листа")
                        fields.add(name)
            except ValueError:
                raise ValueError("Перевірте дужки та назви полів у mail_template.json") from None
            if key == "subject" and (fields - {"ticket_id", "message_number"} or any(c in text for c in "\r\n") or len(text) > 180):
                raise ValueError("Тема листа: один рядок до 180 знаків; дозволені поля ticket_id та message_number")
            if key == "body" and not {"question", "ticket_id"} <= fields:
                raise ValueError("Текст шаблону має містити поля {question} та {ticket_id}")
            if key == 'body':
                self.body_fields = fields

    @classmethod
    def load(cls, path: Path):
        return cls(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else cls()

    def render(self, fields: dict) -> dict:
        values = {key: fields.get(key, '') for key in FIELDS}
        result = {key: value.format_map(values) for key, value in self.data.items()}
        required = {'client_full_name', 'client_company', 'client_email', 'client_phone'}
        if values['client_full_name'] and not required <= self.body_fields:
            result['body'] += ('\n\nКОНТАКТНІ ДАНІ КЛІЄНТА\nПІБ: {client_full_name}\nКомпанія: {client_company}\n'
                               'Електронна пошта: {client_email}\nТелефон: {client_phone}').format_map(values)
        return result


class MailError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        self.permanent = permanent
        super().__init__(message)


class SMTPMailer:
    def __init__(self, settings: MailSettings, password: str):
        settings.validate()
        self.settings, self.password = settings, password

    def send(self, payload: dict):
        settings = self.settings
        if not settings.enabled or not self.password:
            raise MailError("Пошту не налаштовано: перевірте параметри та пароль застосунку", permanent=True)
        message = EmailMessage()
        message["From"] = settings.sender
        message["To"] = settings.recipient
        message["Subject"] = payload["subject"]
        message["Message-ID"] = payload["message_id"]
        message["Date"] = format_datetime(datetime.fromtimestamp(payload["accepted_epoch"], timezone.utc))
        if payload.get('client_email'):
            try:
                validate_address(payload['client_email'])
            except ValueError:
                raise MailError('Некоректна контактна пошта клієнта в листі', permanent=True) from None
            message['Reply-To'] = payload['client_email']
        if payload.get("in_reply_to"):
            message["In-Reply-To"] = payload["in_reply_to"]
            message["References"] = payload["in_reply_to"]
        message.set_content(payload["body"], charset="utf-8")
        connection = None
        try:
            context = ssl.create_default_context()
            if settings.security == "ssl":
                connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=10, context=context)
            else:
                connection = smtplib.SMTP(settings.host, settings.port, timeout=10)
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(settings.username, self.password)
            refused = connection.send_message(message, from_addr=settings.sender, to_addrs=[settings.recipient])
            if refused:
                raise MailError("Поштовий сервер відхилив адресу отримувача", permanent=True)
        except smtplib.SMTPAuthenticationError:
            raise MailError("SMTP не прийняв облікові дані. Перевірте логін і пароль застосунку", permanent=True) from None
        except smtplib.SMTPNotSupportedError:
            raise MailError("SMTP-сервер не підтримує обраний спосіб TLS або входу", permanent=True) from None
        except ssl.SSLError:
            raise MailError("Не вдалося перевірити TLS-з’єднання з поштою. Перевірте SMTP-сервер і час на ПК", permanent=True) from None
        except smtplib.SMTPRecipientsRefused as error:
            codes = [value[0] for value in error.recipients.values()]
            permanent = bool(codes and all(code >= 500 for code in codes))
            raise MailError("SMTP-сервер відхилив отримувача", permanent) from None
        except smtplib.SMTPResponseException as error:
            raise MailError(f"Поштовий сервер відхилив надсилання (SMTP {error.smtp_code})", error.smtp_code >= 500) from None
        except ValueError:
            raise MailError("SMTP не прийняв формат облікових даних. Перевірте логін і пароль застосунку", permanent=True) from None
        except (OSError, smtplib.SMTPException):
            # Never echo SMTP responses or credentials into the application log.
            raise MailError("Тимчасова помилка з’єднання з поштовим сервером") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


class MailDelivery:
    def __init__(self, store: Store, sender, emit):
        self.store, self.sender, self.emit = store, sender, emit
        self.paused = False
        self.wait_until = 0.0

    def resume(self):
        with self.store.db:
            self.store.resume_mail()
        self.paused = False
        self.wait_until = 0

    def flush_one(self):
        if self.paused or time.time() < self.wait_until:
            return False
        job = self.store.ready_mail()
        if not job:
            return False
        try:
            self.sender.send(json.loads(job["payload"]))
        except MailError as error:
            delay = min(300, 5 * 2 ** min(job["attempts"], 6))
            with self.store.db:
                self.store.retry_mail(job["id"], delay, str(error), blocked=error.permanent)
            self.paused = error.permanent
            self.wait_until = time.time() + delay
            self.emit("log", f"Лист для звернення №{job['ticket_id']} залишено в черзі: {error}")
        else:
            with self.store.db:
                self.store.mark_mail_sent(job["id"])
                ticket = self.store.ticket(job["ticket_id"])
                if ticket:
                    self.store.enqueue("sendMessage", {
                        "chat_id": ticket["user_id"],
                        "text": f"Повідомлення зі звернення №{job['ticket_id']} передано поштовому серверу служби підтримки.",
                    }, ticket["id"])
            self.emit("log", f"Лист для звернення №{job['ticket_id']} прийнятий SMTP-сервером")
        self.emit("mail_counts", self.store.mail_counts())
        return True


class MailWorker:
    """SMTP has its own SQLite connection and never blocks Telegram polling or Tk."""
    def __init__(self, database: Path, settings: MailSettings, password: str, emit):
        self.database, self.settings, self.password, self.emit = database, settings, password, emit
        self.stop = threading.Event()
        self.retry_requested = threading.Event()
        self.thread = threading.Thread(target=self.run, name="InfoBotMail", daemon=False)

    def start(self):
        self.thread.start()

    def run(self):
        store = None
        try:
            store = Store(self.database)
            delivery = MailDelivery(store, SMTPMailer(self.settings, self.password), self.emit)
            delivery.resume()
            while not self.stop.is_set():
                if self.retry_requested.is_set():
                    delivery.resume()
                    self.retry_requested.clear()
                delivery.flush_one()
                self.stop.wait(0.75)
        except Exception as error:
            self.emit("log", f"Поштовий потік зупинено ({type(error).__name__}). Звернення збережені; перезапустіть бота після перевірки налаштувань")
        finally:
            if store:
                store.close()
            self.password = ""
