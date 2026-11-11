from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.engine import Engine
from infobot.faq import FAQCatalog, FAQConflict, MAX_ITEMS, load_faq, validate_items, validate_source
from infobot.runner import Runner
from infobot.settings import ROOT, Settings
from infobot.store import Store


class MenuAndFAQTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.settings = Settings(operator_id=900)
        self.catalog = FAQCatalog(load_faq(ROOT / 'faq.json'))
        self.engine = Engine(self.store, self.settings, [], faq_catalog=self.catalog)
        self.sequence = 0

    def tearDown(self):
        self.store.close()

    def message(self, text, uid=900):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'date': int(time.time()), 'text': text,
            'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'}}}
        self.engine.process(update)
        return update

    def click(self, data, uid=900):
        self.sequence += 1
        self.engine.process({'update_id': self.sequence, 'callback_query': {
            'id': str(self.sequence), 'from': {'id': uid}, 'data': data,
            'message': {'chat': {'id': uid, 'type': 'private'}}}})

    def last(self, uid=900):
        row = self.store.db.execute("SELECT payload FROM outbox WHERE destination=? AND method='sendMessage' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return json.loads(row[0])

    def buttons(self, uid=900):
        return [button for row in self.last(uid)['reply_markup']['inline_keyboard'] for button in row]

    def begin_draft(self):
        self.message('/ask', 100)
        self.message('Потрібне уточнення щодо обладнання', 100)

    def test_welcome_has_exactly_five_customer_buttons(self):
        self.message('/start', 100)
        self.assertEqual([b['text'] for b in self.buttons(100)], ['Створення виробництва', 'Розвиток виробництва', 'Відновлення виробництва', 'Допомогти з вибором', 'Звернутися до підтримки'])
        self.assertIn('гранти', self.last(100)['text'])
        self.assertEqual(self.store.counts()[0], 0)

    def test_start_preserves_draft_and_opens_initial_menu(self):
        self.begin_draft()
        self.message('Шевченко Олена Іванівна', 100)
        self.message('/start', 100)
        self.assertEqual(len(self.buttons(100)), 5)
        self.assertEqual(self.store.draft(100)['step'], 'company')
        self.click('support', 100)
        self.assertIn('Крок 3 із 7', self.last(100)['text'])
        self.assertIn('обладнання', self.store.draft(100)['question'])
        self.click('back', 100)
        self.assertEqual(self.store.draft(100)['step'], 'full_name')

    def test_support_opens_unified_intake_and_commands_remain_available(self):
        self.message('/support', 100)
        self.assertEqual({b['text'] for b in self.buttons(100)}, {'Назад', 'Скасувати'})
        self.assertEqual(self.store.draft(100)['step'], 'question')
        self.click('identity', 100)
        self.assertIn('100', self.last(100)['text'])
        self.click('privacy', 100)
        self.assertIn('email, телефон, ПІБ', self.last(100)['text'])

    def test_commands_require_current_dispatcher_for_text_and_callback(self):
        self.click('new', 100)
        for request, value in ((self.message, '/commands'), (self.click, 'commands')):
            request(value, 100)
            self.assertIn('лише спеціалісту', self.last(100)['text'])
            self.assertNotIn('/reply', self.last(100)['text'])
            self.assertIsNotNone(self.store.draft(100))
            request(value, 900)
            self.assertIn('/reply', self.last()['text'])
        self.settings.operator_id = 901
        self.click('commands', 900)
        self.assertIn('лише спеціалісту', self.last()['text'])

    def test_cancel_button_abandons_question_prompt_and_preserves_saved_draft(self):
        self.click('new', 100)
        button = next(button for button in self.buttons(100) if button['text'] == 'Скасувати')
        self.click(button['callback_data'], 100)
        self.message('Запитання після скасування', 100)
        self.assertIsNone(self.store.draft(100))
        self.begin_draft()
        self.click(button['callback_data'], 100)
        self.assertIsNotNone(self.store.draft(100))

    def test_dispatcher_panel_available_only_to_current_dispatcher(self):
        self.message('/start')
        self.assertEqual(len(self.buttons()), 6)
        self.click('admin')
        self.assertTrue({'faqmanage', 'commands'} <= {b['callback_data'] for b in self.buttons()})
        self.click('admin', 100)
        self.assertIn('лише спеціалісту', self.last(100)['text'])

    def test_all_21_questions_are_reachable_in_pages(self):
        identifiers = []
        for page in range(4):
            self.click(f'faqpage:{page}', 100)
            identifiers.extend(b['callback_data'][4:] for b in self.buttons(100) if b.get('callback_data', '').startswith('faq:'))
            self.assertNotIn('faqmanage', [b.get('callback_data') for b in self.buttons(100)])
        self.assertEqual(identifiers, [q['id'] for q in self.engine.faq])
        self.click('faqpage:999999', 100)
        self.assertIn('Сторінка 4 із 4', self.last(100)['text'])

    def test_answer_has_source_date_and_page_back_button(self):
        self.click('faq:business_plan', 100)
        self.assertIn('2026-11-06', self.last(100)['text'])
        self.assertTrue(any(b.get('url', '').startswith('https://') for b in self.buttons(100)))
        self.assertIn('faqpage:1', [b.get('callback_data') for b in self.buttons(100)])
        self.assertNotIn('faqmanage', [b.get('callback_data') for b in self.buttons(100)])

    def test_dispatcher_can_enter_editor_from_faq(self):
        self.click('faq')
        self.assertIn('faqmanage', [b.get('callback_data') for b in self.buttons()])
        self.click('faqmanage')
        self.assertIn('faqadd', [b.get('callback_data') for b in self.buttons()])

    def test_customer_cannot_forge_any_faq_edit_action(self):
        before = self.catalog.snapshot()
        for action in ('faqmanage', 'faqadd', 'faqedit:about', 'faqfield:about:answer', 'faqremove:about', 'faqdelete:about:0'):
            self.click(action, 100)
            self.assertIn('лише спеціалісту', self.last(100)['text'])
        self.message('Чужа відповідь', 100)
        self.assertEqual(before, self.catalog.snapshot())
        self.assertNotIn(100, self.engine.faq_edits)

    def test_add_question_answer_and_optional_source(self):
        self.click('faqadd')
        self.message('Чи є консультація для нашої компанії?')
        self.message('Додаткова відповідь для клієнта.')
        self.assertEqual(len(self.engine.faq), 21)
        update = self.message('—')
        self.assertEqual(len(self.engine.faq), 22)
        self.assertEqual(self.engine.faq[-1]['source_url'], '')
        self.engine.process(update)
        self.assertEqual(len(self.engine.faq), 22)
        self.click('faq:' + self.engine.faq[-1]['id'], 100)
        self.assertIn('Додаткова відповідь', self.last(100)['text'])

    def test_invalid_new_question_keeps_wizard_at_current_field(self):
        self.click('faqadd')
        self.message('Заголовок\nдругий рядок')
        self.assertEqual(self.engine.faq_edits[900]['field'], 'question')
        self.message('Нове запитання')
        self.message('Нова відповідь')
        self.message('http://example.test')
        self.assertEqual(self.engine.faq_edits[900]['field'], 'source_url')
        self.assertEqual(len(self.engine.faq), 21)
        self.message('https://example.test/faq')
        self.assertEqual(self.engine.faq[-1]['source_url'], 'https://example.test/faq')

    def test_dispatcher_can_edit_all_three_fields(self):
        self.click('faqfield:about:question')
        self.message('Для кого створено програму?')
        self.assertEqual(self.catalog.get('about')['question'], 'Для кого створено програму?')
        self.click('faqfield:about:answer')
        self.message('Оновлена відповідь.')
        item = self.catalog.get('about')
        self.assertNotIn('verified_at', item)
        self.assertEqual(item['source_url'], '')
        self.click('faqfield:about:source_url')
        self.message('https://example.test/source')
        self.click('faq:about', 100)
        self.assertIn('Оновлена відповідь.', self.last(100)['text'])
        self.assertIn('Оновлено спеціалістом', self.last(100)['text'])

    def test_faq_answer_accepts_3000_units_outside_ticket_limit(self):
        self.click('faqfield:about:answer')
        self.message('я' * 2500)
        self.assertEqual(self.catalog.get('about')['answer'], 'я' * 2500)

    def test_invalid_answer_does_not_replace_previous_answer(self):
        before = self.catalog.get('about')
        self.click('faqfield:about:answer')
        self.message('я' * 3001)
        self.assertEqual(self.catalog.get('about'), before)
        self.assertIn(900, self.engine.faq_edits)

    def test_delete_requires_confirmation_and_matches_revision(self):
        self.click('faqremove:about')
        self.assertIsNotNone(self.catalog.get('about'))
        data = self.buttons()[0]['callback_data']
        self.catalog.update('eligible', {'question': 'Хто може взяти участь?'}, self.catalog.revision)
        self.click(data)
        self.assertIsNotNone(self.catalog.get('about'))
        self.click('faqremove:about')
        self.click(self.buttons()[0]['callback_data'])
        self.assertIsNone(self.catalog.get('about'))
        self.click('faq:about', 100)
        self.assertIn('вже видалено', self.last(100)['text'])

    def test_edit_conflict_keeps_newer_changes(self):
        self.click('faqfield:about:answer')
        self.catalog.update('about', {'answer': 'Відповідь із локальної програми.'}, 0)
        self.message('Застарілий текст із Telegram.')
        self.assertEqual(self.catalog.get('about')['answer'], 'Відповідь із локальної програми.')
        self.assertIn('FAQ уже змінено', self.last()['text'])
        self.assertNotIn(900, self.engine.faq_edits)

    def test_cancel_and_menu_discard_unpublished_edit(self):
        for cancel in ('/cancel', '/start', '/menu'):
            self.click('faqadd')
            self.message('Незавершене запитання')
            self.message(cancel)
            self.assertNotIn(900, self.engine.faq_edits)
            self.assertEqual(len(self.engine.faq), 21)

    def test_revoked_dispatcher_cannot_finish_an_edit_or_delete(self):
        self.click('faqfield:about:answer')
        self.settings.operator_id = 901
        self.message('Текст колишнього спеціаліста')
        self.assertNotIn(900, self.engine.faq_edits)
        self.click('faqdelete:about:0')
        self.assertIsNotNone(self.catalog.get('about'))

    def test_pairing_new_dispatcher_clears_edit_sessions(self):
        self.click('faqadd')
        code = self.engine.pairing.issue()
        self.message('/claim ' + code, 901)
        self.assertFalse(self.engine.faq_edits)
        self.assertEqual(self.settings.operator_id, 901)

    def test_expired_edit_cannot_publish(self):
        self.click('faqfield:about:answer')
        self.engine.faq_edits[900]['expires'] = 0
        self.message('Запізніла відповідь')
        self.assertIn('Час редагування минув', self.last()['text'])
        self.assertEqual(self.catalog.revision, 0)

    def test_entering_faq_edit_clears_pending_ticket_reply(self):
        with self.store.db:
            ticket_id = self.store.create_ticket(100, 'Клієнт', '')
        self.click(f'reply:{ticket_id}')
        self.click('faqfield:about:answer')
        self.message('Текст лише для FAQ.')
        self.assertEqual(len(self.store.history(ticket_id)), 0)
        self.assertEqual(self.catalog.get('about')['answer'], 'Текст лише для FAQ.')
        self.click(f'reply:{ticket_id}')
        self.assertFalse(self.engine.faq_edits)

    def test_public_faq_does_not_create_ticket_draft_or_email(self):
        for entry in self.engine.faq:
            self.click('faq:' + entry['id'], 100)
        self.assertEqual(self.store.counts()[0], 0)
        self.assertIsNone(self.store.draft(100))
        self.assertEqual(self.store.mail_counts(), (0, 0, 0))

    def test_navigation_cancels_faq_edit_before_ticket_reply(self):
        self.click('faqfield:about:answer')
        self.click('support')
        self.assertFalse(self.engine.faq_edits)
        self.message('Текст після виходу з редагування.')
        self.assertEqual(self.catalog.revision, 0)

    def test_support_navigation_resumes_unsent_question(self):
        self.click('new', 100)
        self.click('support', 100)
        self.message('Питання', 100)
        self.assertEqual(self.store.draft(100)['question'], 'Питання')
        self.assertEqual(self.store.draft(100)['step'], 'full_name')

    def test_write_error_does_not_stop_engine_or_publish_answer(self):
        self.click('faqfield:about:answer')
        before = self.catalog.snapshot()
        with patch.object(self.catalog, 'update', side_effect=OSError('disk full')):
            self.message('Незбережена відповідь.')
        self.assertEqual(self.catalog.snapshot(), before)
        self.assertIn('Не вдалося зберегти FAQ', self.last()['text'])
        self.message('/start', 100)
        self.assertEqual(len(self.buttons(100)), 5)

    def test_backup_contains_live_edited_faq_next_to_database(self):
        with tempfile.TemporaryDirectory() as directory:
            self.catalog.update('about', {'answer': 'Текст для резервної копії.'}, 0)
            events = []
            runner = Runner('', self.settings, lambda *event: events.append(event), Path(directory), self.catalog)
            runner.controls.put('backup')
            runner.handle_controls(self.engine, self.store)
            copies = list((Path(directory) / 'data' / 'backups').glob('*.faq.json'))
            self.assertEqual(len(copies), 1)
            self.assertEqual(load_faq(copies[0])[0]['answer'], 'Текст для резервної копії.')
            self.assertTrue(any(event[0] == 'backup' for event in events))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'faq.json').write_bytes((ROOT / 'faq.json').read_bytes())
        self.catalog = FAQCatalog.open(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_edits_persist_after_restart_and_override_shipped_faq(self):
        self.catalog.update('about', {'answer': 'Збережений текст.'}, 0)
        self.assertEqual(FAQCatalog.open(self.root).get('about')['answer'], 'Збережений текст.')
        self.assertEqual(FAQCatalog.open(self.root).revision, 1)

    def test_two_catalogs_cannot_overwrite_each_other(self):
        second = FAQCatalog.open(self.root)
        self.catalog.update('about', {'answer': 'Перше редагування.'}, 0)
        with self.assertRaises(FAQConflict):
            second.update('about', {'answer': 'Втрачений запис.'}, 0)
        second.reload()
        self.assertEqual(second.get('about')['answer'], 'Перше редагування.')
        second.update('about', {'answer': 'Наступне редагування.'}, 1)
        self.catalog.reload()
        self.assertEqual(self.catalog.get('about')['answer'], 'Наступне редагування.')

    def test_failed_atomic_write_keeps_previous_memory_and_disk(self):
        self.catalog.update('about', {'answer': 'Попередній текст.'}, 0)
        before = self.catalog.snapshot()
        original = self.catalog.path.read_bytes()
        with patch('infobot.faq.atomic_write', side_effect=OSError('offline disk')):
            with self.assertRaises(OSError):
                self.catalog.update('about', {'answer': 'Не збережено.'}, 1)
        self.assertEqual(self.catalog.snapshot(), before)
        self.assertEqual(self.catalog.path.read_bytes(), original)

    def test_invalid_edit_and_last_question_removal_are_rejected(self):
        catalog = FAQCatalog([{'id': 'single', 'question': 'Одне запитання', 'answer': 'Відповідь'}])
        for values in ({'question': ''}, {'answer': ''}, {'question': 'я' * 61}, {'id': 'other'}):
            with self.assertRaises(ValueError):
                catalog.update('single', values, 0)
        with self.assertRaises(ValueError):
            catalog.delete('single', 0)
        self.assertEqual(catalog.revision, 0)

    def test_source_validation_blocks_non_https_credentials_and_non_text(self):
        for value in ('http://example.test', 'javascript:alert(1)', 'https://name:secret@example.test', 'https://example.test/a b', 123):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_source(value)
        self.assertEqual(validate_source('—'), '')

    def test_max_questions_and_unique_identifiers(self):
        items = [{'id': str(i), 'question': 'Питання', 'answer': 'Відповідь'} for i in range(MAX_ITEMS)]
        self.assertEqual(len(validate_items(items)), MAX_ITEMS)
        with self.assertRaises(ValueError):
            validate_items(items + [{'id': 'another', 'question': 'Питання', 'answer': 'Відповідь'}])
        with self.assertRaises(ValueError):
            validate_items([items[0], items[0]])

    def test_snapshot_and_get_are_independent_copies(self):
        items, _ = self.catalog.snapshot()
        items[0]['answer'] = 'Зміна копії'
        item = self.catalog.get('about')
        item['answer'] = 'Інша копія'
        self.assertNotEqual(self.catalog.get('about')['answer'], 'Зміна копії')
        self.assertNotEqual(self.catalog.get('about')['answer'], 'Інша копія')

    def test_corrupted_override_does_not_silently_reset_content(self):
        self.catalog.update('about', {'answer': 'Важливий текст.'}, 0)
        self.catalog.path.write_text('{пошкоджено', encoding='utf-8')
        with self.assertRaises(ValueError):
            FAQCatalog.open(self.root)

    def test_source_change_removes_previous_verification_stamp(self):
        self.catalog.update('about', {'source_url': 'https://example.test/new-source'}, 0)
        self.assertNotIn('verified_at', self.catalog.get('about'))


if __name__ == '__main__':
    unittest.main()
