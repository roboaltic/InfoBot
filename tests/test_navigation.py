from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.engine import Engine
from infobot.faq import FAQCatalog, load_faq
from infobot.navigation import NavigationContent
from infobot.runner import Delivery
from infobot.settings import ROOT, MailSettings, Settings
from infobot.store import Store
from infobot.telegram import APIError


class TelegramModel:
    """Fake Telegram with per-chat messages, real edit/replacement semantics and failures."""
    def __init__(self):
        self.messages = {}
        self.calls = []
        self.next_id = 10000
        self.edit_unavailable = False
        self.fail_send = False

    def call(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == 'answerCallbackQuery':
            return True
        key = (payload['chat_id'], payload.get('message_id'))
        if method == 'editMessageText':
            if self.edit_unavailable or key not in self.messages:
                raise APIError(400, description="Bad Request: message can't be edited")
            self.messages[key] = copy.deepcopy(payload)
            return {'message_id': key[1]}
        if method == 'sendMessage':
            if self.fail_send:
                raise APIError(500)
            self.next_id += 1
            self.messages[(payload['chat_id'], self.next_id)] = copy.deepcopy(payload)
            return {'message_id': self.next_id, 'date': int(time.time())}
        if method == 'editMessageReplyMarkup':
            if key in self.messages:
                self.messages[key]['reply_markup'] = copy.deepcopy(payload['reply_markup'])
            return True
        if method in ('deleteMessage', 'deleteMessages'):
            for identifier in payload.get('message_ids', [payload.get('message_id')]):
                self.messages.pop((payload['chat_id'], identifier), None)
            return True
        raise AssertionError(method)


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = Settings(operator_id=900)
        self.catalog = FAQCatalog(load_faq(ROOT / 'faq.json'))
        self.engine = Engine(self.store, self.settings, [], faq_catalog=self.catalog)
        self.api = TelegramModel()
        self.sequence = 0
        self.delivery = Delivery(self.store, self.api, lambda *args: None)

    def drain(self):
        for _ in range(200):
            if not self.store.ready_jobs(time.time()):
                break
            self.delivery.sent_at.clear()
            self.delivery.flush()

    def message(self, text, uid=100):
        self.sequence += 1
        self.engine.process({'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'date': int(time.time()), 'text': text,
            'from': {'id': uid, 'first_name': 'Тестовий профіль'},
            'chat': {'id': uid, 'type': 'private'}}})
        self.drain()

    def screen(self, uid=100):
        menu = self.store.menu_message(uid)
        return self.api.messages[(uid, menu['message_id'])]

    def buttons(self, uid=100):
        return [button for row in self.screen(uid)['reply_markup']['inline_keyboard'] for button in row]

    def callback(self, data, uid=100, message_id=None, message_extra=None):
        menu = self.store.menu_message(uid)
        message = {'message_id': message_id if message_id is not None else menu['message_id'],
                   'chat': {'id': uid, 'type': 'private'}, **(message_extra or {})}
        self.sequence += 1
        self.engine.process({'update_id': self.sequence, 'callback_query': {
            'id': str(self.sequence), 'from': {'id': uid}, 'data': data, 'message': message}})
        self.drain()

    def click(self, route, uid=100):
        button = next(button for button in self.buttons(uid)
                      if button.get('callback_data', '').split(':', 2)[-1] == route
                      or button.get('callback_data') == route)
        self.callback(button['callback_data'], uid)

    def select(self, direction='develop', ratio='70_30', uid=100):
        self.message('/start', uid)
        self.click('direction:' + direction, uid)
        self.click('ratio:' + ratio, uid)
        self.click('confirm', uid)

    def intake(self, uid=100):
        for text in ('Запитання', 'Шевченко Олена Іванівна', 'ФОП Шевченко', '0671234567', 'olena@example.test'):
            self.message(text, uid)
        self.assertEqual(self.store.draft(uid)['step'], 'review')
        self.click('form_send', uid)

    def test_exact_initial_menu_and_direction_help(self):
        self.message('/start')
        self.assertEqual([b['text'] for b in self.buttons()], [
            'Створення виробництва', 'Розвиток виробництва', 'Відновлення виробництва',
            'Допомогти з вибором', 'Звернутися до підтримки'])
        original = self.store.menu_message(100)['message_id']
        self.click('choose_help')
        self.assertIn('не висновок', self.screen()['text'])
        self.click('home')
        self.assertEqual(self.store.menu_message(100)['message_id'], original)
        self.assertEqual(len(self.buttons()), 5)

    def test_all_nine_combinations_and_all_eleven_subsections(self):
        data = self.engine.content.data
        for direction in data['directions']:
            for ratio in data['ratios']:
                with self.subTest(direction=direction, ratio=ratio):
                    self.select(direction, ratio)
                    self.assertIn(data['directions'][direction] + ' · ' + ratio.replace('_', '/'), self.screen()['text'])
                    self.assertEqual(len(self.buttons()), 7)
                    for section_id, section in data['sections'].items():
                        self.click('section:' + section_id)
                        for answer_id in section['children']:
                            self.click('answer:' + answer_id)
                            self.assertIn('Умови уточнюються', self.screen()['text'])
                            self.assertIn('потребує затвердження', self.screen()['text'])
                            self.click('section:' + section_id)
                            self.assertEqual(self.engine.nav_state(100)['screen'], 'section:' + section_id)
                        self.click('grant')
                    self.assertEqual(self.store.counts()[0], 0)

    def test_ratio_menu_help_and_information_only_confirmation(self):
        self.message('/start')
        self.click('direction:create')
        self.assertEqual([b['text'] for b in self.buttons()], [
            '50% грант · 50% внесок', '70% грант · 30% внесок', '80% грант · 20% внесок',
            'Допомогти визначити умови', 'Назад'])
        self.click('ratio_help')
        self.assertIn('ще не затверджені', self.screen()['text'])
        self.click('ratios')
        self.click('ratio:80_20')
        self.assertIn('Бот не визначає право на грант', self.screen()['text'])
        self.assertFalse(self.engine.nav_state(100)['confirmed'])
        self.click('confirm')
        self.assertTrue(self.engine.nav_state(100)['confirmed'])

    def test_changing_ratio_preserves_direction_changing_direction_resets_ratio(self):
        self.select()
        self.click('change_ratio')
        state = self.engine.nav_state(100)
        self.assertEqual(state['direction'], 'develop')
        self.assertIsNone(state['ratio'])
        self.assertFalse(state['confirmed'])
        self.click('ratio:50_50')
        self.click('confirm')
        self.click('change_direction')
        self.assertIsNone(self.engine.nav_state(100)['ratio'])
        self.click('direction:restore')
        self.assertIsNone(self.engine.nav_state(100)['ratio'])

    def test_choice_and_buttons_survive_database_reopen_and_start_keeps_draft(self):
        self.select()
        self.click('section:after')
        self.click('answer:jobs')
        self.click('support')
        self.message('Запитання')
        self.message('Шевченко Олена Іванівна')
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store, self.settings, [], faq_catalog=self.catalog)
        self.delivery = Delivery(self.store, self.api, lambda *args: None)
        self.assertEqual(self.engine.nav_state(100)['ratio'], '70_30')
        self.click('form_back')
        self.assertEqual(self.store.draft(100)['client_full_name'], 'Шевченко Олена Іванівна')
        self.message('/start')
        self.assertEqual(len(self.buttons()), 5)
        self.assertEqual(self.engine.nav_state(100)['ratio'], '70_30')
        self.assertEqual(self.store.draft(100)['question'], 'Запитання')

    def test_users_have_separate_navigation_drafts_and_ticket_contexts(self):
        self.select('create', '50_50', 100)
        self.select('restore', '80_20', 200)
        self.click('support', 100)
        self.click('support', 200)
        self.intake(100)
        self.assertEqual(self.store.draft(200)['step'], 'question')
        self.intake(200)
        first = json.loads(self.store.user_ticket(100)['grant_context'])
        second = json.loads(self.store.user_ticket(200)['grant_context'])
        self.assertEqual(first['direction'], 'create')
        self.assertEqual(second['direction'], 'restore')
        self.assertEqual(first['ratio'], '50_50')
        self.assertEqual(second['ratio'], '80_20')

    def test_context_in_review_specialist_card_and_mail(self):
        self.settings.mail = MailSettings(enabled=True, host='smtp.example.test', username='bot@example.test', sender='bot@example.test', recipient='support@example.test')
        self.select()
        self.click('section:conditions')
        self.click('answer:expenses')
        self.click('support')
        self.intake()
        ticket = self.store.user_ticket(100)
        context = json.loads(ticket['grant_context'])
        self.assertEqual(context, {'direction': 'develop', 'ratio': '70_30', 'origin': 'answer:expenses'})
        body = json.loads(self.store.db.execute('SELECT payload FROM mail_outbox').fetchone()[0])['body']
        for value in ('Розвиток виробництва', '70% грант · 30% внесок', 'Дозволені витрати'):
            self.assertIn(value, body)
            notifications = [payload['text'] for (uid, _), payload in self.api.messages.items() if uid == 900]
            self.assertTrue(any(value in text for text in notifications))
        self.message('/admin', 900)
        self.callback('ticket:' + str(ticket['id']), 900)
        self.assertIn('Дозволені витрати', self.screen(900)['text'])

    def test_context_is_known_choice_at_submission_and_original_support_origin(self):
        self.select()
        self.click('section:conditions')
        self.click('support')
        self.message('Запитання')
        self.message('/start')
        self.click('direction:restore')
        self.click('ratio:80_20')
        self.click('confirm')
        self.click('support')
        for text in ('Шевченко Олена Іванівна', 'ФОП Шевченко', '0671234567', 'olena@example.test'):
            self.message(text)
        self.click('form_send')
        context = json.loads(self.store.user_ticket(100)['grant_context'])
        self.assertEqual(context, {'direction': 'restore', 'ratio': '80_20', 'origin': 'section:conditions'})

    def test_support_cancel_and_first_step_back_return_to_exact_origin(self):
        self.select()
        self.click('section:application')
        self.click('answer:documents')
        self.click('support')
        self.click('form_back')
        self.assertEqual(self.engine.nav_state(100)['screen'], 'answer:documents')
        self.assertIsNotNone(self.store.draft(100))
        self.click('support')
        self.message('Запитання')
        self.click('form_cancel')
        self.assertIsNone(self.store.draft(100))
        self.assertEqual(self.engine.nav_state(100)['screen'], 'answer:documents')

    def test_support_without_choice_and_close_notification_returns_to_direction_choice(self):
        self.message('/start')
        self.click('support')
        self.intake()
        context = json.loads(self.store.user_ticket(100)['grant_context'])
        self.assertIsNone(context['direction'])
        self.assertIsNone(context['ratio'])
        self.callback('close:1')
        closure = next((identifier, payload) for (uid, identifier), payload in self.api.messages.items()
                       if uid == 100 and 'Олено, дякуємо' in payload['text'])
        identifier, payload = closure
        self.callback('notification_menu', message_id=identifier, message_extra=payload)
        self.assertEqual(len(self.buttons()), 5)

    def test_close_notification_new_and_old_returns_to_selected_grant(self):
        self.select()
        self.click('support')
        self.intake()
        self.callback('close:1')
        closure = next((identifier, payload) for (uid, identifier), payload in self.api.messages.items()
                       if uid == 100 and 'Олено, дякуємо' in payload['text'])
        identifier, payload = closure
        self.callback('notification_menu', message_id=identifier, message_extra=payload)
        self.assertIn('Розвиток виробництва · 70/30', self.screen()['text'])
        legacy = copy.deepcopy(payload)
        legacy['reply_markup'] = {'inline_keyboard': [[{'text': 'Головне меню', 'callback_data': 'menu'}]]}
        self.callback('menu', message_id=identifier, message_extra=legacy)
        self.assertIn('Розвиток виробництва · 70/30', self.screen()['text'])

    def test_same_message_old_generation_cannot_change_selection_or_send_draft(self):
        self.message('/start')
        stale = self.buttons()[0]['callback_data']
        self.click('direction:develop')
        before = self.engine.nav_state(100)
        self.callback(stale)
        self.assertEqual(self.engine.nav_state(100), before)
        self.assertIn('/start', self.api.calls[-1][1]['text'])
        self.click('ratio:70_30')
        self.click('confirm')
        self.click('support')
        self.message('Запитання')
        old_cancel = next(b['callback_data'] for b in self.buttons() if b['text'] == 'Скасувати')
        self.message('Шевченко Олена Іванівна')
        self.callback(old_cancel)
        self.assertEqual(self.store.draft(100)['step'], 'company')

    def test_old_static_message_actions_acknowledge_without_mutating(self):
        self.select()
        old_id = self.store.menu_message(100)['message_id']
        self.api.edit_unavailable = True
        self.click('support')
        self.api.edit_unavailable = False
        self.assertNotEqual(old_id, self.store.menu_message(100)['message_id'])
        before = dict(self.store.draft(100))
        for route in ('new', 'support', 'cancel_draft', 'back', 'faq:about', 'menu'):
            self.callback(route, message_id=old_id)
            self.assertEqual(dict(self.store.draft(100)), before)
            self.assertIn('/start', self.api.calls[-1][1]['text'])

    def test_menu_replacement_send_failure_retry_keeps_new_navigation_usable(self):
        self.message('/start')
        original = self.store.menu_message(100)['message_id']
        self.api.edit_unavailable = True
        self.api.fail_send = True
        self.click('direction:create')
        self.assertEqual(self.store.menu_message(100)['message_id'], original)
        self.assertGreater(self.store.counts()[1], 0)
        self.api.fail_send = False
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET next_attempt=0')
        self.drain()
        replacement = self.store.menu_message(100)['message_id']
        self.assertNotEqual(original, replacement)
        self.assertEqual(self.api.messages[(100, original)]['reply_markup']['inline_keyboard'], [])
        self.api.edit_unavailable = False
        self.click('ratio:50_50')
        self.click('confirm')
        self.assertEqual(self.engine.nav_state(100)['ratio'], '50_50')

    def test_legacy_draft_migrates_without_losing_contacts(self):
        with self.store.db:
            self.store.create_draft(100, 'Старе запитання', '', '', 1, time.time())
            self.store.update_draft(100, client_email='old@example.test', client_phone='+380671234567', client_company='ТОВ "Стара назва"', step='full_name')
        self.message('/continue')
        self.assertEqual(self.store.draft(100)['step'], 'full_name')
        self.message('Шевченко Олена Іванівна')
        self.assertIn('ТОВ "Стара назва"', self.screen()['text'])
        self.assertEqual(self.store.draft(100)['client_email'], 'old@example.test')
        self.message('/cancel')
        self.assertIsNone(self.store.draft(100))

    def test_malformed_old_navigation_has_safe_defaults(self):
        for value in ({'direction': ['bad'], 'ratio': {}}, {'direction': 'old', 'confirmed': True}, {'direction': 'create', 'ratio': 'old', 'revision': 'old', 'screen': []}):
            with self.store.db:
                self.store.save_navigation(100, value)
            self.message('/start')
            self.assertEqual(len(self.buttons()), 5)
            self.assertFalse(self.engine.nav_state(100)['confirmed'])

    def test_faq_filtering_uses_live_catalog_and_edits_persist(self):
        path = Path(self.directory.name) / 'faq_catalog.json'
        self.catalog = FAQCatalog(load_faq(ROOT / 'faq.json'), path)
        self.engine = Engine(self.store, self.settings, [], faq_catalog=self.catalog)
        self.engine.content.data['faq_bindings'] = {'common': ['about'], 'directions': {'develop': ['business_plan']}, 'combinations': {'develop:70_30': ['spending']}}
        self.select()
        self.click('faq:0')
        self.assertEqual(len([b for b in self.buttons() if ':faqanswer:' in b.get('callback_data', '')]), 3)
        self.message('/admin', 900)
        self.callback('faqfield:about:answer', 900)
        self.message('Оновлена відповідь спеціаліста.', 900)
        self.catalog = FAQCatalog(load_faq(ROOT / 'faq.json'), path)
        self.assertEqual(self.catalog.get('about')['answer'], 'Оновлена відповідь спеціаліста.')
        self.engine.faq_catalog = self.catalog
        self.click('faqanswer:about')
        self.assertIn('Оновлена відповідь спеціаліста.', self.screen()['text'])
        self.click('faq:0')
        self.assertEqual(self.engine.nav_state(100)['screen'], 'faq:0')

    def approved_record(self, summary='Узгоджений текст'):
        return dict(self.engine.content.data['answers']['amount']['common'], status='approved',
                    summary=summary, source_url='https://example.test/order#p1', source_point='Пункт 1')

    def test_missing_combination_and_draft_rules_offer_specialist_without_inference(self):
        self.engine.content.data['combinations']['create'].pop('80_20')
        self.message('/start')
        self.click('direction:create')
        self.click('ratio:80_20')
        self.assertIn('Умови уточнюються', self.screen()['text'])
        self.assertIn('Звернутися до підтримки', [b['text'] for b in self.buttons()])
        self.click('confirm')
        self.assertTrue(self.engine.nav_state(100)['confirmed'])
        self.assertEqual(self.engine.nav_state(100)['ratio'], '80_20')

    def test_only_approved_matrix_can_restrict_information_scenario(self):
        record = self.engine.content.data['combinations']['create']['80_20']
        record['availability'] = 'unavailable'
        self.select('create', '80_20')
        self.assertTrue(self.engine.nav_state(100)['confirmed'])
        record.update(self.approved_record(), availability='unavailable')
        self.message('/start')
        self.click('direction:create')
        self.click('ratio:80_20')
        self.assertNotIn('Підтвердити вибір', [b['text'] for b in self.buttons()])
        self.callback(f'n:{self.engine.nav_state(100)["revision"]}:confirm')
        self.assertFalse(self.engine.nav_state(100)['confirmed'])
        self.assertIn('недоступне', self.screen()['text'])

    def test_common_direction_and_ratio_specific_answers_do_not_leak_to_other_scenarios(self):
        answer = self.engine.content.data['answers']['amount']
        answer['common'] = self.approved_record('Загальна узгоджена відповідь')
        answer['overrides'] = {'create': self.approved_record('Лише створення'),
                               'develop:70_30': self.approved_record('Лише розвиток 70/30')}
        for direction, ratio, expected in (('create', '50_50', 'Лише створення'),
                                            ('develop', '70_30', 'Лише розвиток 70/30'),
                                            ('develop', '50_50', 'Загальна узгоджена відповідь')):
            self.select(direction, ratio)
            self.click('section:conditions')
            self.click('answer:amount')
            self.assertIn(expected, self.screen()['text'])
            self.assertIn('Пункт 1', self.screen()['text'])
            self.assertIn('Статус: затверджено', self.screen()['text'])

    def test_configured_help_questions_display_only_after_approval(self):
        self.engine.content.data['selection_questions'] = [
            {'id': 'approved', 'status': 'approved', 'text': 'Узгоджене запитання для самоперевірки?'},
            {'id': 'draft', 'status': 'draft', 'text': 'Неузгоджене запитання?'}]
        self.message('/start')
        self.click('direction:create')
        self.click('ratio_help')
        self.assertIn('Узгоджене запитання', self.screen()['text'])
        self.assertNotIn('Неузгоджене запитання', self.screen()['text'])
        self.assertIsNone(self.engine.nav_state(100)['ratio'])

    def test_all_existing_faq_pages_and_answer_back_keep_source_as_button(self):
        self.select()
        self.click('faq:0')
        seen = []
        for page in range(4):
            seen.extend(b['callback_data'].split(':', 3)[-1] for b in self.buttons() if ':faqanswer:' in b.get('callback_data', ''))
            if page < 3:
                self.click(f'faq:{page + 1}')
        self.assertEqual(seen, [item['id'] for item in self.engine.faq])
        self.click('faqanswer:consult')
        self.assertTrue(any(b.get('url', '').startswith('https://') for b in self.buttons()))
        self.click('faq:3')
        self.assertEqual(self.engine.nav_state(100)['screen'], 'faq:3')

    def test_long_faq_source_does_not_overflow_telegram_text(self):
        self.catalog.update('about', {'answer': 'а' * 3000, 'source_url': 'https://example.test/' + 'x' * 1400}, self.catalog.revision)
        self.select()
        self.click('faq:0')
        self.click('faqanswer:about')
        self.assertLessEqual(len(self.screen()['text'].encode('utf-16-le')) // 2, 4096)
        self.assertTrue(any(len(b.get('url', '')) > 1400 for b in self.buttons()))
        self.assertTrue(all(len(b.get('callback_data', '').encode()) <= 64 for b in self.buttons()))

    def test_faq_support_preserves_question_origin_and_cancel_restores_answer(self):
        self.select()
        self.click('faq:0')
        self.click('faqanswer:about')
        self.click('support')
        self.assertEqual(json.loads(self.store.draft(100)['grant_context'])['origin'], 'faqanswer:about')
        self.click('form_cancel')
        self.assertEqual(self.engine.nav_state(100)['screen'], 'faqanswer:about')
        self.message('/start')
        self.callback('faq:about')
        self.callback('new')
        self.assertEqual(json.loads(self.store.draft(100)['grant_context'])['origin'], 'faqanswer:about')

    def test_legacy_faq_support_without_choice_restores_original_answer(self):
        self.message('/faq')
        self.callback('faq:about')
        self.callback('new')
        self.assertEqual(json.loads(self.store.draft(100)['grant_context'])['origin'], 'legacy_answer:about')
        self.click('form_cancel')
        self.assertIn(self.catalog.get('about')['answer'], self.screen()['text'])

    def test_navigation_transaction_rolls_back_selection_on_queue_failure(self):
        self.message('/start')
        before = self.engine.nav_state(100)
        button = self.buttons()[0]['callback_data']
        with patch.object(self.store, 'enqueue', side_effect=sqlite3.OperationalError('test full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.callback(button)
        self.assertEqual(self.engine.nav_state(100), before)
        self.assertEqual(self.store.offset, 2)

    def test_complete_legacy_draft_requires_explicit_review_and_send(self):
        with self.store.db:
            self.store.create_draft(100, 'Старе запитання', '', '', 1, time.time())
            self.store.update_draft(100, client_email='old@example.test', client_phone='+380671234567',
                                    client_full_name='Шевченко Олена Іванівна', client_first_name='Олена',
                                    client_company='ТОВ "Стара назва"', step='company')
        self.message('/continue')
        self.assertEqual(self.store.draft(100)['step'], 'review')
        self.assertIsNone(self.store.user_ticket(100))
        self.click('form_send')
        self.assertEqual(self.store.user_ticket(100)['client_company'], 'ТОВ "Стара назва"')

    def test_partial_legacy_ticket_organization_is_prefilled(self):
        with self.store.db:
            ticket_id = self.store.create_ticket(100)
            self.store.db.execute('UPDATE tickets SET client_company=? WHERE id=?', ('ТОВ "Стара назва"', ticket_id))
        self.message('Уточнення до старого звернення')
        self.assertEqual(self.store.draft(100)['client_company'], 'ТОВ "Стара назва"')
        self.assertEqual(self.store.draft(100)['ticket_id'], ticket_id)

    def test_permissions_apply_to_versioned_admin_and_intake(self):
        self.settings.allowed_user_ids = [100]
        self.message('/start')
        self.callback(f'n:{self.engine.nav_state(100)["revision"]}:admin')
        self.assertIn('лише спеціалісту', self.screen()['text'])
        self.message('/ask', 200)
        self.assertIsNone(self.store.draft(200))
        self.assertEqual(self.store.navigation(200), {})
        self.message('/admin', 900)
        self.assertIn('Панель спеціаліста', self.screen(900)['text'])


class ContentAndMigrationTests(unittest.TestCase):
    def test_configured_section_buttons_reject_invalid_telegram_titles(self):
        for collection, identifier in (('sections', 'conditions'), ('answers', 'amount')):
            for title in ('', '   ', 'Два\nрядки', '🙂' * 31):
                with self.subTest(collection=collection, title=title):
                    data = copy.deepcopy(NavigationContent().data)
                    data[collection][identifier]['title'] = title
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'navigation.json'
                        path.write_text(json.dumps(data), encoding='utf-8')
                        with self.assertRaises(ValueError):
                            NavigationContent(path)

    def test_unapproved_text_and_missing_combination_never_publish_rules(self):
        content = NavigationContent()
        draft = {'status': 'draft', 'summary': 'НЕПОГОДЖЕНЕ ПРАВИЛО'}
        self.assertNotIn('НЕПОГОДЖЕНЕ', content.record_text(draft))
        self.assertIn('Умови уточнюються', content.record_text({}))

    def test_catalog_validation_and_specific_overrides(self):
        content = NavigationContent()
        record = dict(content.data['answers']['amount']['common'], status='approved', summary='Узгоджений текст', source_url='https://example.test/order#p1', source_point='Пункт 1')
        content.validate_record(record)
        self.assertIn('Узгоджений текст', content.record_text(record))
        for change in ({'source_url': ''}, {'source_url': 'javascript:bad'}, {'source_point': ''}, {'status': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                content.validate_record({**record, **change})

    def test_old_sqlite_schema_migration_preserves_ticket_message_queue_and_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at REAL NOT NULL, client_company TEXT NOT NULL DEFAULT '');
                INSERT INTO tickets VALUES (1,100,1,'ТОВ "Стара назва"');
                CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE, direction TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
                INSERT INTO messages VALUES (1,1,'client','Старе запитання',1);
                CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, payload TEXT NOT NULL, destination INTEGER NOT NULL, ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
                INSERT INTO outbox VALUES (1,'sendMessage','{}',100,1,2,0);
            ''')
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.ticket(1)['client_company'], 'ТОВ "Стара назва"')
                self.assertEqual(store.ticket(1)['grant_context'], '{}')
                self.assertEqual(store.history(1)[0]['body'], 'Старе запитання')
                self.assertEqual(store.db.execute('SELECT attempts FROM outbox').fetchone()[0], 2)
            finally:
                store.close()
            store = Store(path)
            self.assertEqual(store.ticket(1)['client_company'], 'ТОВ "Стара назва"')
            store.close()
