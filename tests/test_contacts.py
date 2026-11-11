from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.contacts import address_name, normalize_company, normalize_email, normalize_full_name, normalize_phone
from infobot.engine import Engine, load_faq
from infobot.mailer import MailError, MailTemplate, SMTPMailer
from infobot.settings import ROOT, Settings, MailSettings
from infobot.store import Store


class ContactFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.settings = Settings(operator_id=900, mail=MailSettings(enabled=True, host='smtp.example.test',
                        username='bot@example.test', sender='bot@example.test', recipient='support@example.test'))
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / 'faq.json'))
        self.sequence = 0

    def tearDown(self):
        self.store.close()

    def message(self, text, uid=100):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {'message_id': self.sequence, 'date': int(time.time()),
                  'from': {'id': uid, 'first_name': 'Псевдонім', 'username': 'alias_user'},
                  'chat': {'id': uid, 'type': 'private'}, 'text': text}}
        self.engine.process(update)
        return update

    def click(self, data, uid=100):
        self.sequence += 1
        self.engine.process({'update_id': self.sequence, 'callback_query': {'id': str(self.sequence),
            'from': {'id': uid}, 'data': data, 'message': {'chat': {'id': uid, 'type': 'private'}}}})

    def payloads(self, uid):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT payload FROM outbox WHERE method='sendMessage' AND destination=? ORDER BY id", (uid,))]

    def start(self):
        self.message('/ask')
        self.message('Питання, яке має бути передано лише після анкети')

    def finish(self, full_name='Шевченко Олена Іванівна'):
        for value in ('olena@example.test', '0671234567', full_name, 'ТОВ "Укрхелп"'):
            self.message(value)
        return self.store.user_ticket(100)['id']

    def test_no_ticket_or_email_or_operator_notification_before_all_fields(self):
        self.start()
        for stage, value in (('email', 'olena@example.test'), ('phone', '0671234567'), ('full_name', 'Шевченко Олена Іванівна')):
            self.assertEqual(self.store.draft(100)['step'], stage)
            self.assertEqual(self.store.counts()[0], 0)
            self.assertEqual(self.store.mail_counts(), (0, 0, 0))
            self.assertEqual(self.payloads(900), [])
            self.message(value)
        self.assertEqual(self.store.counts()[0], 0)
        self.assertEqual(self.store.draft(100)['step'], 'company')
        self.message('ТОВ "Укрхелп"')
        ticket = self.store.user_ticket(100)
        self.assertEqual(ticket['client_email'], 'olena@example.test')
        self.assertEqual(ticket['client_phone'], '+380671234567')
        self.assertEqual(ticket['client_full_name'], 'Шевченко Олена Іванівна')
        self.assertEqual(ticket['client_first_name'], 'Олена')
        self.assertEqual(ticket['client_company'], 'ТОВ "Укрхелп"')
        self.assertEqual(self.store.mail_counts(), (1, 0, 0))
        self.assertIsNone(self.store.draft(100))

    def test_bad_input_cannot_skip_required_fields(self):
        self.start()
        self.message('ТОВ "Укрхелп"')
        self.assertEqual(self.store.draft(100)['step'], 'email')
        self.message('not-an-email')
        self.assertEqual(self.store.draft(100)['step'], 'email')
        self.message('olena@example.test')
        self.message('Телефон відсутній')
        self.assertEqual(self.store.draft(100)['step'], 'phone')
        self.message('+380671234567')
        self.message('Олена')
        self.assertEqual(self.store.draft(100)['step'], 'full_name')
        self.message('Шевченко Олена Іванівна')
        self.message('Укрхелп')
        self.assertEqual(self.store.draft(100)['step'], 'company')
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.message('тов «Укрхелп»')
        self.assertEqual(self.store.user_ticket(100)['client_company'], 'ТОВ "Укрхелп"')

    def test_back_replaces_value_and_no_stale_form_prompts_survive_submission(self):
        self.start()
        self.message('wrong@example.test')
        self.message('/back')
        self.assertEqual(self.store.draft(100)['step'], 'email')
        self.message('correct@example.test')
        for value in ('+380671234567', 'Шевченко Олена Іванівна', 'ТОВ "Укрхелп"'):
            self.message(value)
        self.assertEqual(self.store.user_ticket(100)['client_email'], 'correct@example.test')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox WHERE draft_user_id IS NOT NULL').fetchone()[0], 0)

    def test_cancel_and_delete_me_erase_partial_personal_data(self):
        for command in ('/cancel', '/delete_me'):
            with self.subTest(command=command):
                self.start()
                for value in ('private@example.test', '+380671234567', 'Шевченко Олена Іванівна'):
                    self.message(value)
                self.message(command)
                self.assertIsNone(self.store.draft(100))
                self.assertIsNone(self.store.user_ticket(100))
                self.assertEqual(self.store.mail_counts(), (0, 0, 0))
                serialized = str(self.payloads(900)) + str(self.payloads(100))
                self.assertNotIn('private@example.test', serialized)
                self.assertNotIn('Шевченко Олена Іванівна', serialized)

    def test_my_ticket_reports_unsent_draft(self):
        self.start()
        self.message('/mine')
        self.assertIn('ще не зареєстроване', self.payloads(100)[-1]['text'])
        self.assertEqual(self.store.counts()[0], 0)

    def test_duplicate_update_does_not_treat_same_email_as_phone(self):
        self.start()
        update = self.message('olena@example.test')
        self.engine.process(update)
        self.assertEqual(self.store.draft(100)['step'], 'phone')
        self.assertEqual(self.store.draft(100)['client_phone'], '')

    def test_restarting_engine_resumes_exact_step_and_original_question(self):
        self.start()
        self.message('olena@example.test')
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / 'faq.json'))
        self.message('/start')
        self.assertIn('Оберіть розділ', self.payloads(100)[-1]['text'])
        self.message('/continue')
        self.assertIn('Крок 2 із 4', self.payloads(100)[-1]['text'])
        for value in ('+380671234567', 'Шевченко Олена Іванівна', 'ТОВ "Укрхелп"'):
            self.message(value)
        self.assertEqual(self.store.history(1)[0]['body'], 'Питання, яке має бути передано лише після анкети')

    def test_finalization_failure_keeps_all_previous_answers_for_retry(self):
        self.start()
        for value in ('olena@example.test', '+380671234567', 'Шевченко Олена Іванівна'):
            self.message(value)
        offset = self.store.offset
        with patch.object(self.store, 'enqueue_mail', side_effect=sqlite3.OperationalError('test full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.message('ТОВ "Укрхелп"')
        self.assertEqual(self.store.offset, offset)
        self.assertEqual(self.store.draft(100)['step'], 'company')
        self.assertEqual(self.store.draft(100)['client_full_name'], 'Шевченко Олена Іванівна')
        self.assertIsNone(self.store.user_ticket(100))
        self.message('ТОВ "Укрхелп"')
        self.assertEqual(self.store.counts()[0], 1)
        self.assertEqual(self.store.mail_counts(), (1, 0, 0))

    def test_mail_and_operator_card_include_all_mandatory_values(self):
        self.start()
        self.finish()
        row = self.store.db.execute('SELECT payload FROM mail_outbox').fetchone()
        payload = json.loads(row[0])
        for value in ('olena@example.test', '+380671234567', 'Шевченко Олена Іванівна', 'ТОВ "Укрхелп"'):
            self.assertIn(value, payload['body'])
            self.assertIn(value, self.payloads(900)[-1]['text'])
        self.assertEqual(payload['client_email'], 'olena@example.test')
        self.click('ticket:1', 900)
        self.assertIn('ТОВ "Укрхелп"', self.payloads(900)[-1]['text'])

    def test_close_addresses_client_by_entered_first_name_not_telegram_alias(self):
        self.start()
        ticket_id = self.finish()
        self.click(f'close:{ticket_id}', 900)
        reply = self.payloads(100)[-1]['text']
        self.assertTrue(reply.startswith('Олено, дякуємо за звернення!'))
        self.assertNotIn('Псевдонім', reply)
        self.assertNotIn('Шевченко,', reply)
        self.assertIsNone(self.store.user_ticket(100))
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))

    def test_client_can_close_and_receives_male_vocative(self):
        self.start()
        ticket_id = self.finish('Коваль Андрій Петрович')
        self.click(f'close:{ticket_id}')
        self.assertTrue(self.payloads(100)[-1]['text'].startswith('Андрію,'))

    def test_new_ticket_collects_contacts_again_after_previous_is_deleted(self):
        self.start()
        ticket_id = self.finish()
        self.click(f'close:{ticket_id}')
        self.start()
        self.assertEqual(self.store.draft(100)['step'], 'email')
        self.assertEqual(self.store.draft(100)['client_email'], '')

    def test_followup_reuses_complete_contacts_in_existing_ticket(self):
        self.start()
        ticket_id = self.finish()
        self.message('Уточнення до звернення')
        self.assertIsNone(self.store.draft(100))
        self.assertEqual(len(self.store.history(ticket_id)), 2)
        self.assertEqual(self.store.mail_counts(), (2, 0, 0))

    def test_legacy_followup_is_held_until_contacts_then_keeps_ticket_number(self):
        with self.store.db:
            ticket_id = self.store.create_ticket(100, 'Старе ім’я')
            self.store.add_message(ticket_id, 'client', 'Старе запитання')
        self.message('Нове уточнення до старого звернення')
        self.assertEqual(self.store.draft(100)['ticket_id'], ticket_id)
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.finish()
        self.assertEqual(self.store.user_ticket(100)['id'], ticket_id)
        self.assertEqual(len(self.store.history(ticket_id)), 2)

    def test_cancel_legacy_contact_form_preserves_previous_ticket(self):
        with self.store.db:
            ticket_id = self.store.create_ticket(100)
            self.store.add_message(ticket_id, 'client', 'Старе запитання')
        self.message('Відкладене уточнення')
        self.message('olena@example.test')
        self.message('/cancel')
        self.assertIsNotNone(self.store.ticket(ticket_id))
        self.assertEqual(len(self.store.history(ticket_id)), 1)
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))

    def test_other_user_cannot_modify_or_cancel_draft(self):
        self.start()
        self.click('cancel_draft', 200)
        self.click('back', 200)
        self.message('fake@example.test', 200)
        self.assertEqual(self.store.draft(100)['step'], 'email')
        self.assertEqual(self.store.draft(100)['client_email'], '')

    def test_contact_messages_are_tracked_for_telegram_deletion(self):
        self.start()
        ticket_id = self.finish()
        ids = {row[0] for row in self.store.db.execute('SELECT message_id FROM telegram_refs WHERE ticket_id=? AND chat_id=100', (ticket_id,))}
        self.assertEqual(ids, {2, 3, 4, 5, 6})

    def test_form_completes_when_mail_disabled_but_dispatcher_connected(self):
        self.settings.mail.enabled = False
        self.start()
        self.finish()
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))
        self.assertIn('Шевченко Олена Іванівна', self.payloads(900)[-1]['text'])

    def test_draft_persists_in_backup_and_new_connection(self):
        self.start()
        self.message('olena@example.test')
        with tempfile.TemporaryDirectory() as directory:
            path = self.store.backup(Path(directory))
            restored = Store(path)
            self.assertEqual(restored.draft(100)['step'], 'phone')
            self.assertEqual(restored.draft(100)['client_email'], 'olena@example.test')
            self.assertEqual(restored.mail_counts(), (0, 0, 0))
            restored.close()


class ContactValidationTests(unittest.TestCase):
    def test_email_validation_and_header_injection_rejection(self):
        self.assertEqual(normalize_email('name+test@EXAMPLE.TEST'), 'name+test@example.test')
        for value in ('invalid', 'one@example.test,two@example.test', 'a@example.test\r\nBcc: x@example.test', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_email(value)

    def test_phone_formats_and_invalid_values(self):
        for value in ('0671234567', '380671234567', '+38 (067) 123-45-67'):
            self.assertEqual(normalize_phone(value), '+380671234567')
        self.assertEqual(normalize_phone('+48 123 456 789'), '+48123456789')
        for value in ('немає', '123', '+0123456789', '+380abcdefghi', '+1234567890123456'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_phone(value)

    def test_full_name_order_and_compound_name(self):
        self.assertEqual(normalize_full_name('  Шевченко  Олена Іванівна  '), ('Шевченко Олена Іванівна', 'Олена'))
        self.assertEqual(normalize_full_name('Коваль Анна-Марія —')[1], 'Анна-Марія')
        self.assertEqual(normalize_full_name('Лук’яненко Дар’я Юріївна')[1], "Дар'я")
        for value in ('Олена', 'Шевченко О. І.', 'Шевченко Олена123 Іванівна', 'Шевченко\nОлена Іванівна'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_full_name(value)

    def test_company_requires_legal_form_name_and_balanced_quotes(self):
        for value in ('ТОВ "Укрхелп"', 'тов «Укрхелп»', 'ТОВ “Укрхелп”'):
            self.assertEqual(normalize_company(value), 'ТОВ "Укрхелп"')
        for value in ('Укрхелп', 'ТОВ Укрхелп', 'ТОВ ""', 'ТОВ "---"', 'ТОВ "Укрхелп»', 'ТОВ "Один"\nТОВ "Два"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_company(value)

    def test_personal_address_forms_and_unknown_names(self):
        for name, expected in (('Олена', 'Олено'), ('Андрій', 'Андрію'), ('Ігор', 'Ігорю'),
                               ('Анна-Марія', 'Анно-Маріє'), ('Jane', 'Jane')):
            self.assertEqual(address_name(name), expected)

    def test_old_custom_email_template_cannot_omit_mandatory_contacts(self):
        template = MailTemplate({'subject': 'Звернення {ticket_id}', 'body': 'Запитання {ticket_id}: {question}'})
        result = template.render({'ticket_id': 1, 'question': 'Питання', 'client_full_name': 'Шевченко Олена Іванівна',
            'client_company': 'ТОВ "Укрхелп"', 'client_email': 'olena@example.test', 'client_phone': '+380671234567'})
        for value in ('Шевченко Олена Іванівна', 'ТОВ "Укрхелп"', 'olena@example.test', '+380671234567'):
            self.assertIn(value, result['body'])

    def test_reply_to_uses_validated_client_email_without_changing_recipient(self):
        settings = MailSettings(enabled=True, host='smtp.example.test', username='bot@example.test',
                                sender='bot@example.test', recipient='support@example.test')
        payload = {'subject': 'Звернення', 'body': 'Питання', 'message_id': '<x@example.test>',
                   'accepted_epoch': time.time(), 'client_email': 'client@example.test'}
        with patch('infobot.mailer.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.send_message.return_value = {}
            SMTPMailer(settings, 'LOCAL_FAKE_PASSWORD').send(payload)
            args = smtp.return_value.send_message.call_args
            self.assertEqual(str(args.args[0]['Reply-To']), 'client@example.test')
            self.assertEqual(args.kwargs['to_addrs'], ['support@example.test'])
        payload['client_email'] = 'client@example.test\r\nBcc: fake@example.test'
        with patch('infobot.mailer.smtplib.SMTP_SSL') as smtp, self.assertRaises(MailError):
            SMTPMailer(settings, 'LOCAL_FAKE_PASSWORD').send(payload)
        smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
