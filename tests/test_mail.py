from __future__ import annotations

import ast
import json
import os
import smtplib
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.engine import Engine, load_faq
from infobot.mailer import MailDelivery, MailError, MailTemplate, MailWorker, SMTPMailer
from infobot.settings import MailSettings, ROOT, Settings, load_secret, save_secret
from infobot.store import Store


def mail_settings():
    return MailSettings(enabled=True, host='smtp.example.test', username='bot@example.test',
                        sender='bot@example.test', recipient='support@example.test')


class FakeSender:
    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        if self.error:
            raise self.error
        self.sent.append(payload.copy())


class MailFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.settings = Settings(operator_id=900, mail=mail_settings())
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / 'faq.json'), bot_username='test_infobot')
        self.sequence = 0
        self.sender = FakeSender()
        self.events = []
        self.delivery = MailDelivery(self.store, self.sender, lambda *event: self.events.append(event))

    def tearDown(self):
        self.store.close()

    def message(self, text, uid=100, **actor):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'date': int(time.time()),
            'from': {'id': uid, 'first_name': 'Олена', 'last_name': 'Тестова', 'username': 'olena_test', **actor},
            'chat': {'id': uid, 'type': 'private'}, 'text': text}}
        self.engine.process(update)
        return update

    def create(self):
        self.message('/ask')
        self.message('Після довідки залишилося запитання. Як отримати допомогу?')
        update = self.finish_contacts()
        return self.store.user_ticket(100)['id'], update

    def finish_contacts(self):
        update = None
        for text in ('Шевченко Олена Іванівна', 'ТОВ "Укрхелп"', '+380671234567', 'olena@example.test'):
            update = self.message(text)
        self.sequence += 1
        update = {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence),
            'from': {'id': 100}, 'data': f'n:{self.engine.nav_state(100)["revision"]}:form_send',
            'message': {'chat': {'id': 100, 'type': 'private'}}}}
        self.engine.process(update)
        return update

    def queued(self):
        return self.store.db.execute('SELECT * FROM mail_outbox ORDER BY id').fetchall()

    def replies(self):
        return [json.loads(row[0])['text'] for row in self.store.db.execute("SELECT payload FROM outbox WHERE method='sendMessage'")]

    def test_question_creates_ukrainian_letter_with_identity_and_template(self):
        ticket_id, update = self.create()
        self.assertEqual(self.store.mail_counts(), (1, 0, 0))
        payload = json.loads(self.queued()[0]['payload'])
        self.assertEqual(payload['subject'], f'[InfoBot] Звернення №{ticket_id}')
        self.assertIn('Олена Тестова', payload['body'])
        self.assertIn('@olena_test', payload['body'])
        self.assertIn('Telegram ID: 100', payload['body'])
        self.assertIn('Як отримати допомогу?', payload['body'])
        self.assertIn('Нове звернення', payload['body'])
        self.assertIn('/reply 1', payload['body'])
        self.assertIn('@test_infobot', payload['body'])
        self.assertFalse(payload['in_reply_to'])

    def test_faq_and_unsubmitted_draft_never_create_letters(self):
        self.message('/start')
        self.engine.callback(100, 'faq:about')
        self.message('/ask')
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))

    def test_mail_only_mode_without_dispatcher_accepts_question(self):
        self.settings.operator_id = 0
        self.create()
        self.assertEqual(self.store.counts()[0], 1)
        self.assertEqual(self.store.mail_counts()[0], 1)
        self.assertFalse(self.store.db.execute('SELECT id FROM outbox WHERE destination=0 AND method=\'sendMessage\'').fetchall())

    def test_disabled_mail_keeps_telegram_flow_without_backfilling(self):
        self.settings.mail.enabled = False
        ticket_id, update = self.create()
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.settings.mail.enabled = True
        self.message('Нове доповнення після ввімкнення пошти')
        self.assertEqual(self.store.mail_counts(), (1, 0, 0))
        self.assertIn('Нове доповнення', json.loads(self.queued()[0]['payload'])['body'])

    def test_no_channels_does_not_accept_unserviceable_ticket(self):
        self.settings.mail.enabled = False
        self.settings.operator_id = 0
        self.message('/ask')
        self.message('Не повинно створюватися')
        self.assertEqual(self.store.counts()[0], 0)

    def test_followups_use_same_subject_and_mail_thread(self):
        self.create()
        self.message('Друге повідомлення клієнта')
        first, second = [json.loads(row['payload']) for row in self.queued()]
        self.assertEqual(first['subject'], second['subject'])
        self.assertEqual(second['in_reply_to'], first['message_id'])
        self.assertNotEqual(first['message_id'], second['message_id'])
        self.assertIn('Доповнення до звернення', second['body'])
        self.assertIn('Повідомлення клієнта: №2', second['body'])

    def test_duplicate_telegram_update_does_not_duplicate_mail(self):
        ticket_id, update = self.create()
        self.engine.process(update)
        self.assertEqual(len(self.queued()), 1)

    def test_mail_enqueue_failure_rolls_back_question_and_offset(self):
        self.message('/ask')
        self.message('Питання')
        for value in ('Шевченко Олена Іванівна', 'ФОП Шевченко', '0671234567', 'olena@example.test'):
            self.message(value)
        previous_offset = self.store.offset
        with patch.object(self.store, 'enqueue_mail', side_effect=sqlite3.OperationalError('test full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.process({'update_id': self.sequence + 1, 'callback_query': {
                    'id': 'send', 'from': {'id': 100},
                    'data': f'n:{self.engine.nav_state(100)["revision"]}:form_send',
                    'message': {'chat': {'id': 100, 'type': 'private'}}}})
        self.assertEqual(self.store.offset, previous_offset)
        self.assertEqual(self.store.counts()[0], 0)
        self.assertEqual(self.store.draft(100)['step'], 'review')

    def test_client_gets_queued_notice_before_smtp_acceptance(self):
        self.create()
        self.assertTrue(any('поставлено в чергу' in text for text in self.replies()))
        self.assertFalse(any('передано поштовому серверу' in text for text in self.replies()))
        self.delivery.flush_one()
        self.assertEqual(self.store.mail_counts(), (0, 1, 0))
        self.assertTrue(any('передано поштовому серверу' in text for text in self.replies()))
        self.assertEqual(self.queued()[0]['payload'], '{}')

    def test_temporary_failure_preserves_mail_and_retry_succeeds(self):
        self.create()
        self.sender.error = MailError('Тимчасове тестове відключення')
        self.delivery.flush_one()
        row = self.queued()[0]
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_attempt'], time.time())
        self.assertFalse(any('передано поштовому серверу' in text for text in self.replies()))
        self.delivery.resume()
        self.sender.error = None
        self.delivery.flush_one()
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.store.mail_counts(), (0, 1, 0))

    def test_permanent_failure_pauses_without_losing_question(self):
        ticket_id, update = self.create()
        self.message('Додаткова інформація')
        self.sender.error = MailError('Перевірте пароль застосунку', permanent=True)
        self.delivery.flush_one()
        self.assertTrue(self.delivery.paused)
        self.assertFalse(self.delivery.flush_one())
        self.assertEqual(self.sender.calls, 1)
        self.assertEqual(self.store.mail_counts(), (1, 0, 1))
        self.assertEqual(len(self.store.history(ticket_id)), 2)
        self.delivery.resume()
        self.sender.error = None
        self.delivery.flush_one()
        self.delivery.flush_one()
        self.assertEqual(self.store.mail_counts(), (0, 2, 0))

    def test_closing_cancels_pending_mail_and_erases_local_identity(self):
        ticket_id, update = self.create()
        self.engine.callback(100, f'close:{ticket_id}')
        self.store.db.commit()
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.assertIsNone(self.store.ticket(ticket_id))
        self.assertFalse(self.delivery.flush_one())
        self.assertEqual(self.sender.sent, [])

    def test_sent_mail_is_not_recalled_when_ticket_closes(self):
        ticket_id, update = self.create()
        self.delivery.flush_one()
        self.engine.callback(100, f'close:{ticket_id}')
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.assertEqual(len(self.sender.sent), 1)

    def test_operator_replies_do_not_create_client_question_emails(self):
        ticket_id, update = self.create()
        self.message(f'/reply {ticket_id} Відповідь від спеціаліста', uid=900)
        self.assertEqual(len(self.queued()), 1)

    def test_cards_show_mail_status_and_deny_other_users(self):
        ticket_id, update = self.create()
        self.engine.callback(100, 'mine')
        self.assertIn('у черзі — 1', self.replies()[-1])
        self.engine.callback(200, f'ticket:{ticket_id}')
        self.assertIn('недоступне', self.replies()[-1])

    def test_unicode_newlines_and_braces_are_preserved_in_email_body(self):
        self.message('/ask')
        question = 'Питання українською 👋\nBcc: attacker@example.test\n{client_id}'
        self.message(question, first_name='Ірина\r\nTo: injection@example.test')
        self.finish_contacts()
        payload = json.loads(self.queued()[0]['payload'])
        self.assertIn(question, payload['body'])
        self.assertNotIn('\n', payload['subject'])

    def test_legacy_ticket_gets_valid_root_when_mail_is_enabled_later(self):
        self.settings.mail.enabled = False
        self.create()
        self.settings.mail.enabled = True
        self.message('Нове повідомлення')
        self.message('Наступне повідомлення')
        first, second = [json.loads(row['payload']) for row in self.queued()]
        self.assertFalse(first['in_reply_to'])
        self.assertEqual(second['in_reply_to'], first['message_id'])


class SMTPTests(unittest.TestCase):
    def setUp(self):
        self.settings = mail_settings()
        self.payload = {'subject': '[InfoBot] Звернення №1', 'body': 'Українське запитання 👋\nBcc: nobody@example.test',
                        'message_id': '<message@example.test>', 'in_reply_to': '<root@example.test>', 'accepted_epoch': 1793793600}
        self.password = 'LOCAL_TEST_PASSWORD_NOT_A_REAL_SECRET'
        self.connection = type('Connection', (), {})()
        self.events = []
        self.connection.ehlo = lambda: self.events.append('ehlo')
        self.connection.starttls = lambda **kwargs: self.events.append(('tls', kwargs['context'].check_hostname))
        self.connection.login = lambda user, password: self.events.append(('login', user, password))
        self.connection.close = lambda: self.events.append('close')
        def send(message, **kwargs):
            self.sent_message, self.envelope = message, kwargs
            self.events.append('send')
            return {}
        self.connection.send_message = send

    def test_ssl_uses_verified_tls_and_fixed_recipient(self):
        with patch('infobot.mailer.smtplib.SMTP_SSL', return_value=self.connection) as smtp:
            SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertTrue(smtp.call_args.kwargs['context'].check_hostname)
        self.assertEqual(smtp.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(self.envelope['to_addrs'], ['support@example.test'])
        self.assertEqual(self.sent_message['From'], 'bot@example.test')
        self.assertEqual(self.sent_message['To'], 'support@example.test')
        self.assertIsNone(self.sent_message['Bcc'])
        self.assertIn('Українське запитання', self.sent_message.get_content())
        self.assertEqual(self.sent_message['In-Reply-To'], '<root@example.test>')
        self.assertEqual(self.sent_message['Message-ID'], '<message@example.test>')

    def test_starttls_must_succeed_before_login(self):
        self.settings.security, self.settings.port = 'starttls', 587
        with patch('infobot.mailer.smtplib.SMTP', return_value=self.connection):
            SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertLess(self.events.index(('tls', True)), self.events.index(('login', self.settings.username, self.password)))

    def test_failed_tls_never_sends_credentials(self):
        self.settings.security = 'starttls'
        self.connection.starttls = lambda **kwargs: (_ for _ in ()).throw(ssl.SSLError('certificate'))
        with patch('infobot.mailer.smtplib.SMTP', return_value=self.connection):
            with self.assertRaises(MailError) as error:
                SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertTrue(error.exception.permanent)
        self.assertFalse(any(isinstance(event, tuple) and event[0] == 'login' for event in self.events))

    def test_authentication_error_never_exposes_server_response_or_password(self):
        self.connection.login = lambda *args: (_ for _ in ()).throw(smtplib.SMTPAuthenticationError(535, self.password.encode()))
        with patch('infobot.mailer.smtplib.SMTP_SSL', return_value=self.connection):
            with self.assertRaises(MailError) as error:
                SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertTrue(error.exception.permanent)
        self.assertNotIn(self.password, str(error.exception))

    def test_temporary_and_permanent_smtp_failures_are_classified(self):
        for code, permanent in ((421, False), (450, False), (550, True), (554, True)):
            with self.subTest(code=code):
                self.connection.send_message = lambda *args, **kwargs: (_ for _ in ()).throw(smtplib.SMTPDataError(code, b'private details'))
                with patch('infobot.mailer.smtplib.SMTP_SSL', return_value=self.connection):
                    with self.assertRaises(MailError) as error:
                        SMTPMailer(self.settings, self.password).send(self.payload)
                self.assertEqual(error.exception.permanent, permanent)
                self.assertNotIn('private details', str(error.exception))

    def test_unencodable_authentication_is_a_safe_permanent_error(self):
        self.connection.login = lambda *args: (_ for _ in ()).throw(UnicodeEncodeError('ascii', 'таємниця', 0, 1, 'secret'))
        with patch('infobot.mailer.smtplib.SMTP_SSL', return_value=self.connection):
            with self.assertRaises(MailError) as error:
                SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertTrue(error.exception.permanent)
        self.assertNotIn('таємниця', str(error.exception))

    def test_recipient_refusal_is_not_reported_as_success(self):
        self.connection.send_message = lambda *args, **kwargs: {'support@example.test': (550, b'refused')}
        with patch('infobot.mailer.smtplib.SMTP_SSL', return_value=self.connection):
            with self.assertRaises(MailError) as error:
                SMTPMailer(self.settings, self.password).send(self.payload)
        self.assertTrue(error.exception.permanent)

    def test_disabled_or_missing_password_cannot_send(self):
        with patch('infobot.mailer.smtplib.SMTP_SSL') as smtp:
            with self.assertRaises(MailError):
                SMTPMailer(self.settings, '').send(self.payload)
            self.settings.enabled = False
            with self.assertRaises(MailError):
                SMTPMailer(self.settings, self.password).send(self.payload)
        smtp.assert_not_called()


class MailStorageTests(unittest.TestCase):
    def test_old_database_migrates_without_losing_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.executescript('''
                    CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL UNIQUE,created_at REAL NOT NULL);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,direction TEXT NOT NULL,body TEXT NOT NULL,created_at REAL NOT NULL);
                    CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    INSERT INTO tickets VALUES (7,100,1);
                    INSERT INTO messages VALUES (9,7,'client','Старе звернення',1);
                    INSERT INTO meta VALUES ('offset','123');
                ''')
            store = Store(path)
            self.assertEqual(store.offset, 123)
            self.assertEqual(store.ticket(7)['user_id'], 100)
            self.assertEqual(store.history(7)[0]['body'], 'Старе звернення')
            self.assertEqual(store.ticket(7)['client_name'], '')
            self.assertEqual(store.mail_counts(), (0, 0, 0))
            store.close()

    def test_old_configuration_defaults_to_mail_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{"operator_id":900,"allowed_user_ids":[100],"delete_telegram_on_close":true}')
            settings = Settings.load(path)
            self.assertEqual(settings.operator_id, 900)
            self.assertFalse(settings.mail.enabled)

    def test_backup_preserves_mail_queue_and_does_not_include_password(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'bot.sqlite3')
            with store.db:
                ticket = store.create_ticket(100, 'Тестовий клієнт')
                row = store.add_message(ticket, 'client', 'Питання')
                store.enqueue_mail(ticket, row, {'body': 'Питання', 'subject': 'Звернення'})
            path = store.backup(Path(directory) / 'backups')
            store.close()
            restored = Store(path)
            self.assertEqual(restored.mail_counts(), (1, 0, 0))
            self.assertEqual(json.loads(restored.ready_mail()['payload'])['body'], 'Питання')
            self.assertNotIn('password', str(restored.db.execute("SELECT name FROM sqlite_master").fetchall()))
            restored.close()

    def test_smtp_secret_is_stored_separately_from_json(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'INFOBOT_SMTP_PASSWORD': ''}):
            secret = Path(directory) / 'smtp.secret'
            password = 'TEST_APP_PASSWORD'
            save_secret(secret, password)
            self.assertEqual(load_secret(secret, 'INFOBOT_SMTP_PASSWORD'), password)
            settings = Settings(mail=mail_settings())
            path = Path(directory) / 'config.json'
            settings.save(path)
            self.assertNotIn(password, path.read_text())
            self.assertEqual(Settings.load(path).mail.recipient, 'support@example.test')

    def test_worker_uses_independent_connection_and_leaves_telegram_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bot.sqlite3'
            store = Store(path)
            with store.db:
                ticket = store.create_ticket(100)
                row = store.add_message(ticket, 'client', 'Тест')
                store.enqueue_mail(ticket, row, {'body': 'Тест'})
            sent = threading.Event()
            worker = MailWorker(path, mail_settings(), 'LOCAL_FAKE_PASSWORD', lambda *event: None)
            class Sender:
                def send(self, payload):
                    sent.set()
                    worker.stop.set()
            with patch('infobot.mailer.SMTPMailer', return_value=Sender()):
                worker.start()
                self.assertTrue(sent.wait(3))
                worker.thread.join(3)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(store.mail_counts(), (0, 1, 0))
            self.assertEqual(store.counts()[1], 1)
            store.close()


class ValidationTests(unittest.TestCase):
    def test_template_rejects_unknown_fields_header_injection_and_missing_question(self):
        cases = [
            {'subject': 'Тема', 'body': '{ticket_id} {unknown}'},
            {'subject': 'Тема\nBcc: fake@example.test', 'body': '{ticket_id} {question}'},
            {'subject': '{question}', 'body': '{ticket_id} {question}'},
            {'subject': 'Тема', 'body': '{ticket_id}'},
            {'subject': 'Тема', 'body': '{ticket_id.__class__} {question}'},
            {'subject': 'Тема', 'body': '{ticket_id} {question!r}'},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                MailTemplate(case)

    def test_mail_settings_reject_unsafe_or_incomplete_configuration(self):
        for key, value in (('recipient', 'one@example.test,two@example.test'), ('sender', 'bot@example.test\r\nBcc: evil@example.test'),
                           ('security', 'plain'), ('port', 0), ('host', 'https://smtp.example.test'), ('username', '')):
            settings = mail_settings()
            setattr(settings, key, value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                settings.validate()

    def test_ukrainian_user_interface_contains_no_russian_specific_letters(self):
        paths = list((ROOT / 'infobot').glob('*.py')) + [ROOT / 'main.py', ROOT / 'faq.json', ROOT / 'mail_template.json']
        for path in paths:
            if path.suffix == '.py':
                texts = [node.value for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            else:
                texts = [path.read_text(encoding='utf-8')]
            for text in texts:
                self.assertFalse(set('ыэёъЫЭЁЪ') & set(text), f'{path.name}: {text}')


if __name__ == '__main__':
    unittest.main()
