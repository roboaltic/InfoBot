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
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.settings = Settings(operator_id=900, mail=MailSettings(enabled=True, host='smtp.example.test',
                        username='bot@example.test', sender='bot@example.test', recipient='support@example.test'))
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / 'faq.json'))
        self.sequence = 0

    def message(self, text, uid=100):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {'message_id': self.sequence, 'date': int(time.time()),
                  'from': {'id': uid, 'first_name': 'Псевдонім', 'username': 'alias_user'},
                  'chat': {'id': uid, 'type': 'private'}, 'text': text}}
        self.engine.process(update)
        return update

    def click(self, route, uid=100):
        self.sequence += 1
        data = f'n:{self.engine.nav_state(uid)["revision"]}:{route}' if route.startswith('form_') else route
        self.engine.process({'update_id': self.sequence, 'callback_query': {'id': str(self.sequence),
            'from': {'id': uid}, 'data': data, 'message': {'chat': {'id': uid, 'type': 'private'}}}})

    def payloads(self, uid):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT payload FROM outbox WHERE method='sendMessage' AND destination=? ORDER BY id", (uid,))]

    def start(self):
        self.message('/ask')
        self.message('Питання, яке має бути передано лише після анкети')

    def review(self, full_name='Шевченко Олена Іванівна'):
        for value in (full_name, 'ТОВ "Укрхелп"', '0671234567', 'olena@example.test'):
            self.message(value)

    def finish(self, full_name='Шевченко Олена Іванівна'):
        self.review(full_name)
        self.click('form_send')
        return self.store.user_ticket(100)['id']

    def test_no_ticket_mail_or_notification_before_explicit_send(self):
        self.start()
        for step,value in (('full_name','Шевченко Олена Іванівна'),('company','ФОП Шевченко Олена'),('phone','0671234567'),('email','olena@example.test')):
            self.assertEqual(self.store.draft(100)['step'],step)
            self.assertEqual(self.store.counts()[0],0)
            self.assertEqual(self.store.mail_counts(),(0,0,0))
            self.assertEqual(self.payloads(900),[])
            self.message(value)
        self.assertEqual(self.store.draft(100)['step'],'review')
        self.assertIsNone(self.store.user_ticket(100))
        self.message('Надіслати')
        self.assertIsNone(self.store.user_ticket(100))
        self.click('form_send')
        ticket=self.store.user_ticket(100)
        self.assertEqual(ticket['client_company'],'ФОП Шевченко Олена')
        self.assertEqual(ticket['client_phone'],'+380671234567')
        self.assertEqual(self.store.mail_counts(),(1,0,0))

    def test_bad_input_keeps_current_step(self):
        self.start()
        self.message('Олена')
        self.assertEqual(self.store.draft(100)['step'],'full_name')
        self.message('Шевченко Олена Іванівна')
        self.message('x')
        self.assertEqual(self.store.draft(100)['step'],'company')
        self.message('ФОП Шевченко')
        self.message('no phone')
        self.assertEqual(self.store.draft(100)['step'],'phone')
        self.message('0671234567')
        self.message('bad email')
        self.assertEqual(self.store.draft(100)['step'],'email')

    def test_back_keeps_values_and_allows_correction(self):
        self.start()
        self.review()
        self.click('form_back')
        self.assertEqual(self.store.draft(100)['step'],'email')
        self.assertEqual(self.store.draft(100)['client_email'],'olena@example.test')
        self.message('correct@example.test')
        self.click('form_send')
        self.assertEqual(self.store.user_ticket(100)['client_email'],'correct@example.test')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox WHERE draft_user_id IS NOT NULL').fetchone()[0],0)

    def test_edit_question_and_contacts_then_submit(self):
        self.start()
        self.review()
        self.click('form_edit')
        self.assertEqual(self.store.draft(100)['step'],'question')
        self.assertEqual(self.store.draft(100)['client_company'],'ТОВ "Укрхелп"')
        self.message('Виправлене запитання')
        self.finish()
        self.assertEqual(self.store.history(1)[0]['body'],'Виправлене запитання')

    def test_unregistered_business(self):
        self.start()
        self.message('Шевченко Олена Іванівна')
        self.click('form_unregistered')
        self.assertEqual(self.store.draft(100)['step'],'phone')
        self.message('0671234567')
        self.message('olena@example.test')
        self.click('form_send')
        self.assertEqual(self.store.user_ticket(100)['client_company'],'Бізнес ще не зареєстровано')

    def test_cancel_and_delete_me_erase_partial_personal_data(self):
        for command in ('/cancel','/delete_me'):
            self.start()
            self.review()
            self.message(command)
            self.assertIsNone(self.store.draft(100))
            self.assertIsNone(self.store.user_ticket(100))
            serialized=str(self.payloads(900))+str(self.payloads(100))
            self.assertNotIn('olena@example.test',serialized)
            self.assertNotIn('Шевченко Олена Іванівна',serialized)

    def test_duplicate_update_does_not_advance_twice(self):
        self.start()
        update=self.message('Шевченко Олена Іванівна')
        self.engine.process(update)
        self.assertEqual(self.store.draft(100)['step'],'company')
        self.assertEqual(self.store.draft(100)['client_company'],'')

    def test_restart_and_start_preserve_draft_and_contacts(self):
        self.start()
        self.message('Шевченко Олена Іванівна')
        self.engine=Engine(self.store,self.settings,load_faq(ROOT/'faq.json'))
        self.message('/start')
        self.assertIn('Оберіть напрям',self.payloads(100)[-1]['text'])
        self.message('/continue')
        self.assertIn('Крок 3 із 7',self.payloads(100)[-1]['text'])
        for value in ('ТОВ "Укрхелп"','0671234567','olena@example.test'): self.message(value)
        self.click('form_send')
        self.assertEqual(self.store.history(1)[0]['body'],'Питання, яке має бути передано лише після анкети')

    def test_finalization_failure_keeps_review_for_retry(self):
        self.start()
        self.review()
        offset=self.store.offset
        with patch.object(self.store,'enqueue_mail',side_effect=sqlite3.OperationalError('test full')):
            with self.assertRaises(sqlite3.OperationalError): self.click('form_send')
        self.assertEqual(self.store.offset,offset)
        self.assertEqual(self.store.draft(100)['step'],'review')
        self.assertEqual(self.store.draft(100)['client_full_name'],'Шевченко Олена Іванівна')
        self.assertIsNone(self.store.user_ticket(100))
        self.click('form_send')
        self.assertEqual(self.store.counts()[0],1)
        self.assertEqual(self.store.mail_counts(),(1,0,0))

    def test_mail_and_specialist_card_include_contacts_and_missing_context(self):
        self.start()
        self.finish()
        payload=json.loads(self.store.db.execute('SELECT payload FROM mail_outbox').fetchone()[0])
        for value in ('olena@example.test','+380671234567','Шевченко Олена Іванівна','ТОВ "Укрхелп"','Напрям: Не обрано','Співфінансування: Не обрано'):
            self.assertIn(value,payload['body'])
            self.assertIn(value,self.payloads(900)[-1]['text'])
        self.click('ticket:1',900)
        self.assertIn('ТОВ "Укрхелп"',self.payloads(900)[-1]['text'])

    def test_close_addresses_client_by_entered_first_name(self):
        self.start()
        ticket_id=self.finish()
        self.click(f'close:{ticket_id}',900)
        self.assertTrue(self.payloads(100)[-1]['text'].startswith('Олено, дякуємо'))
        self.assertIsNone(self.store.user_ticket(100))

    def test_client_can_close_with_male_vocative(self):
        self.start()
        ticket_id=self.finish('Шевченко Андрій Іванович')
        self.click(f'close:{ticket_id}')
        self.assertTrue(self.payloads(100)[-1]['text'].startswith('Андрію, дякуємо'))

    def test_new_ticket_collects_contacts_again(self):
        self.start()
        ticket_id=self.finish()
        self.click(f'close:{ticket_id}',900)
        self.start()
        self.assertEqual(self.store.draft(100)['client_email'],'')
        self.assertGreater(self.finish(),ticket_id)

    def test_followup_reuses_contacts(self):
        self.start()
        ticket_id=self.finish()
        self.message('Доповнення')
        self.assertIsNone(self.store.draft(100))
        self.assertEqual(len(self.store.history(ticket_id)),2)
        self.assertEqual(self.store.mail_counts(),(2,0,0))

    def test_legacy_followup_keeps_number_and_organization(self):
        with self.store.db:
            ticket_id=self.store.create_ticket(100,'Старе ім’я')
            self.store.db.execute('UPDATE tickets SET client_company=? WHERE id=?',('ТОВ "Стара назва"',ticket_id))
            self.store.add_message(ticket_id,'client','Старе запитання')
        self.message('Уточнення')
        self.assertEqual(self.store.draft(100)['ticket_id'],ticket_id)
        self.finish()
        self.assertEqual(self.store.user_ticket(100)['id'],ticket_id)
        self.assertEqual(len(self.store.history(ticket_id)),2)

    def test_cancel_legacy_form_preserves_ticket(self):
        with self.store.db:
            ticket_id=self.store.create_ticket(100)
            self.store.add_message(ticket_id,'client','Старе запитання')
        self.message('Уточнення')
        self.message('/cancel')
        self.assertIsNotNone(self.store.ticket(ticket_id))
        self.assertEqual(len(self.store.history(ticket_id)),1)
        self.assertEqual(self.store.mail_counts(),(0,0,0))

    def test_other_user_cannot_modify_or_cancel_draft(self):
        self.start()
        self.click('cancel_draft',200)
        self.click('back',200)
        self.message('fake@example.test',200)
        self.assertEqual(self.store.draft(100)['step'],'full_name')
        self.assertEqual(self.store.draft(100)['client_email'],'')

    def test_contact_messages_tracked_for_deletion(self):
        self.start()
        ticket_id=self.finish()
        ids={row[0] for row in self.store.db.execute('SELECT message_id FROM telegram_refs WHERE ticket_id=? AND chat_id=100',(ticket_id,))}
        self.assertEqual(ids,{2,3,4,5,6})

    def test_telegram_only_mode(self):
        self.settings.mail.enabled=False
        self.start()
        self.finish()
        self.assertEqual(self.store.mail_counts(),(0,0,0))
        self.assertIn('Шевченко Олена Іванівна',self.payloads(900)[-1]['text'])

    def test_unconnected_support_keeps_review(self):
        self.settings.mail.enabled=False
        self.settings.operator_id=0
        self.start()
        self.review()
        self.click('form_send')
        self.assertIsNone(self.store.user_ticket(100))
        self.assertEqual(self.store.draft(100)['step'],'review')

    def test_draft_backup_and_new_connection(self):
        self.start()
        self.message('Шевченко Олена Іванівна')
        path=self.store.backup(self.path.parent/'backup')
        restored=Store(path)
        try:
            self.assertEqual(restored.draft(100)['step'],'company')
            self.assertEqual(restored.draft(100)['client_full_name'],'Шевченко Олена Іванівна')
            self.assertEqual(restored.mail_counts(),(0,0,0))
        finally: restored.close()


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
