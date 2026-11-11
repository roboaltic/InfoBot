from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from infobot.engine import Engine, text_size
from infobot.faq import FAQCatalog, load_faq
from infobot.navigation import NavigationContent
from infobot.runner import Delivery
from infobot.screens import ScreenCatalog
from infobot.settings import ROOT, Settings
from infobot.store import Store
from test_navigation import TelegramModel


class DocumentFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'bot.sqlite3'
        self.faq_path = Path(self.directory.name)/'faq_catalog.json'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = Settings(operator_id=900, delete_telegram_on_close=False)
        self.api = TelegramModel()
        self.seq = 0
        self.connect()

    def connect(self):
        self.catalog = FAQCatalog(load_faq(ROOT/'faq.json'), self.faq_path)
        self.engine = Engine(self.store, self.settings, [], faq_catalog=self.catalog,
                             document_content=ScreenCatalog())
        self.delivery = Delivery(self.store, self.api, lambda *args: None)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.connect()

    def drain(self):
        for _ in range(200):
            if not self.store.ready_jobs(time.time()):
                return
            self.delivery.sent_at.clear()
            self.delivery.flush()
        self.fail('Черга не завершилася')

    def message(self, text, uid=100):
        self.seq += 1
        self.engine.process({'update_id':self.seq, 'message':{
            'message_id':self.seq,'date':time.time(),'from':{'id':uid},
            'chat':{'id':uid,'type':'private'}, 'text':text}})
        self.drain()

    def callback(self, data, uid=100, message_id=None):
        self.seq += 1
        menu = self.store.menu_message(uid)
        self.engine.process({'update_id':self.seq,'callback_query':{
            'id':str(self.seq),'from':{'id':uid},'data':data,'message':{
                'message_id':message_id if message_id is not None else menu['message_id'],
                'chat':{'id':uid,'type':'private'}}}})
        self.drain()

    def screen(self, uid=100):
        return self.api.messages[(uid,self.store.menu_message(uid)['message_id'])]

    def buttons(self, uid=100):
        buttons = [button for row in self.screen(uid)['reply_markup']['inline_keyboard'] for button in row]
        for button in buttons:
            self.assertLessEqual(len(button.get('callback_data','').encode('utf-8')),64)
        return buttons

    def click(self, route, uid=100):
        button = next(b for b in self.buttons(uid) if b.get('callback_data','').split(':',2)[-1]==route or b.get('callback_data')==route)
        self.callback(button['callback_data'],uid)

    def select(self, direction='D02', ratio='R70', uid=100):
        self.message('/start',uid)
        self.click('s:'+direction,uid)
        self.assertEqual(self.engine.nav_state(uid)['screen'],'s:'+direction)
        self.click('s:R00',uid)
        self.click('s:'+ratio,uid)
        self.assertIn('Перейти до меню гранту', [button['text'] for button in self.buttons(uid)])
        self.assertNotIn('інформаційний вибір', self.screen(uid)['text'])
        self.click('s:M02',uid)

    def review(self, uid=100):
        for value in ('Моє запитання','Шевченко Олена Іванівна','ФОП Шевченко','+493012345678','olena@example.test'):
            self.message(value,uid)
        self.assertEqual(self.store.draft(uid)['step'],'review')

    def ticket(self):
        self.select()
        self.click('support')
        self.review()
        self.click('form_send')
        return self.store.user_ticket(100)

    def test_document_all_combinations_and_information_sections_have_real_links(self):
        for direction in ('D01','D02','D03'):
            for ratio in ('R50','R70','R80'):
                with self.subTest(direction=direction,ratio=ratio):
                    self.select(direction,ratio)
                    self.assertEqual(len(self.buttons()),7)
                    for section, children in (('F00',('F01','F02','F03')),
                                               ('A00',('A01','A03','A04','A05')),
                                               ('B00',('B01','B02','B03','B04'))):
                        self.click('s:'+section)
                        for sid in children:
                            self.click('s:'+sid)
                            text = self.screen()['text']
                            self.assertIn('Порядок, затверджений постановою КМУ від 24.06.2022 №739',text)
                            self.assertNotRegex(text,r'\bS[0-8]\b|screen_id|На затвердження|потребує звірення')
                            self.assertTrue(any(b.get('url','').startswith('https:') for b in self.buttons()))
                            self.assertLessEqual(text_size(text),4096)
                            self.click('doc_back')
                            self.assertEqual(self.engine.nav_state(100)['screen'],'s:'+section)
                        self.click('doc_back')
                    self.assertEqual(self.store.counts()[0],0)

    def test_damage_documents_back_returns_to_actual_origin_before_grant_confirmation(self):
        self.message('/start')
        self.click('s:D03')
        self.click('s:A02')
        self.click('doc_back')
        self.assertEqual(self.engine.nav_state(100)['screen'],'s:D03')
        self.select('D03','R80')
        self.click('s:A00')
        self.click('s:A01')
        self.click('s:A02')
        self.restart()
        self.click('doc_back')
        self.assertEqual(self.engine.nav_state(100)['screen'],'s:A01')

    def test_helper_collects_description_without_eligibility_and_resets_on_direction_change(self):
        self.message('/start')
        self.click('s:D01')
        self.click('s:R00')
        self.click('s:H01')
        self.message('Київська область, громада; виробництво за іншою адресою')
        self.click('doc_help:H02:0')
        self.click('doc_help:H03:0')
        state = self.engine.nav_state(100)
        self.assertEqual(state['screen'],'s:H04')
        self.assertEqual(len(state['help_answers']),3)
        self.assertFalse(state['confirmed'])
        self.assertIsNone(state['ratio'])
        self.assertIn('Автоматичний висновок',self.screen()['text'])
        self.restart()
        self.click('s:R70')
        self.click('s:M02')
        self.click('doc_ratio')
        self.assertEqual(self.engine.nav_state(100)['direction'],'create')
        self.click('doc_direction')
        state=self.engine.nav_state(100)
        self.assertEqual(state['help_answers'],{})
        self.assertIsNone(state['direction'])
        self.assertIsNone(state['ratio'])

    def test_support_snapshot_edit_each_field_returns_directly_to_review(self):
        self.select()
        self.click('s:F00')
        self.click('s:F03')
        self.click('support')
        self.review()
        expected = {'question':'Нове запитання','full_name':'Іваненко Марія —','company':'ТОВ Приклад',
                    'phone':'+33123456789','email':'new@example.test'}
        for field, value in expected.items():
            self.click('form_edit')
            self.assertIn('Оберіть поле', self.screen()['text'])
            self.click('form_field_'+field)
            self.message(value)
            self.assertEqual(self.store.draft(100)['step'],'review')
        self.message('/start')
        self.select('D03','R80')
        self.click('support')
        self.assertIn('Відновлення виробництва',self.screen()['text'])
        old_send = next(b['callback_data'] for b in self.buttons() if b['text']=='Надіслати')
        self.click('form_send')
        ticket=self.store.user_ticket(100)
        self.assertEqual(ticket['client_email'],'new@example.test')
        self.assertEqual(ticket['client_full_name'],'Іваненко Марія —')
        snapshot=json.loads(ticket['grant_context'])
        self.assertEqual(snapshot['direction'],'restore')
        self.assertEqual(snapshot['ratio'],'80_20')
        self.assertEqual(snapshot['origin'],'s:F03')
        self.assertTrue(ticket['submission_id'])
        self.callback(old_send)
        self.assertEqual(self.store.counts()[0],1)
        self.assertEqual(len(self.store.history(ticket['id'])),1)

    def test_support_no_choice_unregistered_cancel_back_and_restart_edit(self):
        self.message('/ask')
        self.message('Питання')
        self.message('Шевченко Олена Іванівна')
        self.click('form_unregistered')
        self.message('+380671234567')
        self.message('olena@example.test')
        self.click('form_edit')
        self.click('form_field_phone')
        self.restart()
        self.message('bad')
        self.assertEqual(self.store.draft(100)['step'],'phone')
        self.assertIn('Перевірте значення',self.screen()['text'])
        self.click('form_back')
        self.assertEqual(self.store.draft(100)['step'],'review')
        self.assertEqual(self.store.draft(100)['client_company'],'Бізнес ще не зареєстровано')
        self.assertIn('Не обрано',self.screen()['text'])
        self.click('form_cancel')
        self.assertIsNone(self.store.draft(100))
        self.assertEqual(self.store.counts()[0],0)
        self.assertIn('скасовано',self.screen()['text'])
        self.click('s:M01')
        self.assertEqual(len(self.buttons()),5)

    def test_open_dialog_navigation_does_not_add_messages_and_specialist_accepts_once(self):
        ticket=self.ticket()
        notice_id=next(mid for (uid,mid),p in self.api.messages.items() if uid==900 and 'Звернення №' in p['text'])
        self.message('/admin',900)
        self.callback('accept:'+str(ticket['id']),900,message_id=notice_id)
        self.assertEqual(self.store.ticket(ticket['id'])['accepted_by'],900)
        self.callback('accept:'+str(ticket['id']),900,message_id=notice_id)
        accepted=[p for (uid,_),p in self.api.messages.items() if uid==100 and 'Спеціаліст прийняв' in p['text']]
        self.assertEqual(len(accepted),1)
        self.assertFalse(any('попереднє меню' in p.get('text','') for method,p in self.api.calls if method=='answerCallbackQuery'))
        self.click('s:M02')
        self.click('s:B00')
        self.assertEqual(len(self.store.history(ticket['id'])),1)
        self.message('Уточнення клієнта')
        self.assertEqual(len(self.store.history(ticket['id'])),2)
        self.restart()
        self.assertEqual(self.store.ticket(ticket['id'])['accepted_by'],900)
        self.callback('close:'+str(ticket['id']))
        closure_id=next(mid for (uid,mid),p in self.api.messages.items() if uid==100 and 'завершено' in p['text'])
        self.callback('notification_menu',message_id=closure_id)
        self.assertEqual(self.engine.nav_state(100)['screen'],'s:M02')
        self.callback('notification_support',message_id=closure_id)
        self.assertIsNotNone(self.store.draft(100))

    def test_faq_four_per_page_live_edit_bindings_persist_and_delete_is_not_resurrected(self):
        self.select()
        self.click('s:Q00')
        self.assertEqual(len([b for b in self.buttons() if ':s:Q0' in b.get('callback_data','')]),4)
        self.click('doc_page:1')
        self.click('s:Q08')
        self.click('doc_back')
        self.assertEqual(self.engine.nav_state(100)['faq_page'],1)
        self.message('/admin',900)
        self.callback('faqfield:q01:answer',900)
        self.message('Оновлений графік. Уточніть на офіційному сайті.',900)
        self.callback('faqfield:q01:directions',900)
        self.message('restore',900)
        self.restart()
        self.assertEqual(self.catalog.get('q01')['directions'],['*'])
        self.assertEqual(self.catalog.preview('q01')['directions'],['restore'])
        self.callback('faqapprove:q01:'+str(self.catalog.revision),900)
        self.assertEqual(self.catalog.get('q01')['directions'],['restore'])
        self.click('doc_page:0')
        self.assertFalse(any(':s:Q01' in b.get('callback_data','') for b in self.buttons()))
        self.select('D03','R80')
        self.click('s:Q00')
        self.click('s:Q01')
        self.assertIn('Оновлений графік',self.screen()['text'])
        self.assertNotIn('2026-11-11',self.screen()['text'])
        self.assertFalse(any('url' in b for b in self.buttons()))
        self.catalog.delete('q01',self.catalog.revision)
        self.restart()
        self.assertIsNone(self.catalog.get('q01'))
        self.assertEqual(len([i for i in self.catalog.snapshot()[0] if not i.get('screen_id')]),21)

    def test_replacement_send_failure_stale_clients_and_admins_and_cleanup_failure(self):
        self.select()
        original=self.store.menu_message(100)['message_id']
        stale=self.buttons()[0]['callback_data']
        self.api.edit_unavailable=True
        self.api.fail_send=True
        self.click('s:F00')
        self.assertEqual(self.store.menu_message(100)['message_id'],original)
        self.api.fail_send=False
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET next_attempt=0')
        self.drain()
        self.api.edit_unavailable=False
        self.assertNotEqual(self.store.menu_message(100)['message_id'],original)
        state=self.engine.nav_state(100)
        self.callback(stale,message_id=original)
        self.assertEqual(self.engine.nav_state(100),state)
        ticket=self.ticket()
        self.message('/admin',900)
        self.callback('ticket:'+str(ticket['id']),900)
        old_admin=self.store.menu_message(900)['message_id']
        self.message('/start',900)
        self.callback('close:'+str(ticket['id']),900,message_id=old_admin)
        self.assertIsNotNone(self.store.ticket(ticket['id']))
        self.callback('accept:'+str(ticket['id']))
        self.assertEqual(self.store.ticket(ticket['id'])['accepted_by'],0)

    def test_privacy_url_and_long_review_have_one_keyboard_and_no_markup_interpretation(self):
        self.settings.privacy_policy_url='https://example.test/privacy'
        self.message('/ask')
        self.review()
        self.assertTrue(any(b.get('url')==self.settings.privacy_policy_url for b in self.buttons()))
        with self.store.db:
            state=self.engine.nav_state(100)
            state['help_answers']={'H01':'а'*500,'H02':'б'*500,'H03':'в'*500}
            self.store.save_navigation(100,state)
            self.store.update_draft(100,question='<b>'+('🙂'*996)+'</b>',
                                   client_full_name='А'*60+' '+'Б'*60+' '+'В'*60,
                                   client_company='Г'*180, grant_context=json.dumps({
                'direction':'develop','ratio':'70_30','origin':'s:F03',
                'help_answers':{'H01':'а'*500,'H02':'б'*500,'H03':'в'*500}}))
        self.api.calls.clear()
        self.message('/continue')
        text_calls=[p for method,p in self.api.calls if method in ('sendMessage','editMessageText')]
        self.assertGreater(len(text_calls),1)
        self.assertEqual(sum(bool(p.get('reply_markup')) for p in text_calls),1)
        self.assertTrue(all(text_size(p['text'])<=4096 and 'parse_mode' not in p for p in text_calls))

    def test_unconnected_submission_retains_draft_and_atomic_queue_failure_preserves_submission(self):
        self.settings.operator_id=0
        self.message('/ask')
        self.review()
        self.click('form_send')
        self.assertIn('поки не надіслано',self.screen()['text'])
        self.assertEqual(self.store.counts()[0],0)
        submission=self.store.draft(100)['submission_id']
        self.settings.operator_id=900
        self.click('form_retry')
        with patch.object(self.store,'enqueue',side_effect=sqlite3.OperationalError('test full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.click('form_send')
        self.assertEqual(self.store.draft(100)['submission_id'],submission)
        self.assertEqual(self.store.counts()[0],0)
        self.click('form_send')
        self.assertEqual(self.store.user_ticket(100)['submission_id'],submission)

    def test_unapproved_faq_is_not_public_and_unprivileged_approval_does_not_publish(self):
        self.select()
        before=self.catalog.get('q02')['answer']
        self.message('/admin',900)
        self.callback('faqfield:q02:answer',900)
        self.message('Неопублікований правовий текст',900)
        self.click('s:Q00')
        self.click('s:Q02')
        self.assertIn(before,self.screen()['text'])
        self.assertNotIn('Неопублікований',self.screen()['text'])
        self.callback('faqapprove:q02:'+str(self.catalog.revision))
        self.assertEqual(self.catalog.get('q02')['answer'],before)
        self.assertIsNotNone(self.catalog.get('q02').get('pending'))
        self.restart()
        self.assertEqual(self.catalog.preview('q02')['answer'],'Неопублікований правовий текст')

    def test_preserved_faq_bindings_apply_to_legacy_list_and_direct_answer(self):
        self.select('D01','R50')
        self.catalog.update('about',{'directions':['restore']},self.catalog.revision)
        self.catalog.approve('about',self.catalog.revision)
        self.click('s:Q00')
        self.assertFalse(any(b.get('callback_data')=='faq:about' for b in self.buttons()))
        self.callback('faq:about')
        self.assertNotIn(self.catalog.get('about')['answer'],self.screen()['text'])
        self.select('D03','R80')
        self.click('s:Q00')
        self.click('doc_legacy')
        self.assertTrue(any(b.get('callback_data')=='faq:about' for b in self.buttons()))
        self.click('faq:about')
        self.assertIn(self.catalog.get('about')['answer'],self.screen()['text'])
        self.click('new')
        self.click('form_cancel')
        self.click('s:legacy_answer:about')
        self.assertIn(self.catalog.get('about')['answer'],self.screen()['text'])
        self.click('faqpage:0')
        self.assertTrue(any(b.get('callback_data')=='faq:about' for b in self.buttons()))

    def test_unchanged_menu_and_rejected_old_keyboard_cleanup_do_not_duplicate_replacement(self):
        from infobot.telegram import APIError
        self.select()
        self.click('s:F00')
        original_call=self.api.call
        def call(method,payload):
            if method=='editMessageText':
                raise APIError(400,description='message is not modified')
            return original_call(method,payload)
        menu_id=self.store.menu_message(100)['message_id']
        with patch.object(self.api,'call',side_effect=call):
            self.click('s:F01')
        self.assertEqual(self.store.menu_message(100)['message_id'],menu_id)
        self.api.edit_unavailable=True
        def cleanup_fails(method,payload):
            if method=='editMessageReplyMarkup':
                raise APIError(400,description="message can't be edited")
            return original_call(method,payload)
        with patch.object(self.api,'call',side_effect=cleanup_fails):
            self.message('/start')
        replacement=self.store.menu_message(100)['message_id']
        self.assertNotEqual(replacement,menu_id)
        self.api.edit_unavailable=False
        self.click('s:D01')
        self.assertEqual(self.store.menu_message(100)['message_id'],replacement)
        self.assertEqual(self.store.counts()[1],0)

    def test_two_users_have_independent_helpers_forms_and_navigation(self):
        self.select('D01','R50',100)
        self.select('D03','R80',200)
        self.click('support',100)
        self.review(100)
        self.click('support',200)
        self.message('Інший запит',200)
        self.click('form_send',100)
        self.assertIsNone(self.store.user_ticket(200))
        self.assertEqual(self.store.draft(200)['question'],'Інший запит')
        self.assertEqual(self.engine.nav_state(100)['ratio'],'50_50')
        self.assertEqual(self.engine.nav_state(200)['ratio'],'80_20')


class DocumentCatalogTests(unittest.TestCase):
    def test_invalid_catalog_links_routes_and_substitutions_are_rejected(self):
        for field in ('url','route','placeholder'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                data=copy.deepcopy(ScreenCatalog().data)
                if field=='url':
                    data['sources']['S1']['url']='javascript:bad'
                elif field=='route':
                    data['screens']['M01']['buttons'][0]['target']='UNKNOWN'
                else:
                    data['screens']['F01']['body']='Текст {невідоме_поле}'
                path=Path(directory)/'screens.json'
                path.write_text(json.dumps(data),encoding='utf-8')
                path.with_name('faq_v01.json').write_bytes((ROOT/'content/faq_v01.json').read_bytes())
                path.with_name('faq_v02.json').write_bytes((ROOT/'content/faq_v02.json').read_bytes())
                with self.assertRaises(ValueError):
                    ScreenCatalog(path)

    def test_preview_buttons_use_their_source_message_after_text_input(self):
        from infobot.demo import Demo
        updates=[]
        demo=SimpleNamespace(update_id=1,message_ids={1:99},button_message_ids={1:42},
                             engine=SimpleNamespace(process=updates.append),drain=lambda:None)
        Demo.click(demo,1,'menu')
        self.assertEqual(updates[0]['callback_query']['message']['message_id'],42)
        self.assertTrue(Demo.call(demo,'editMessageReplyMarkup',{'chat_id':1,'message_id':1,'reply_markup':{}}))
        self.assertEqual(demo.button_message_ids[1],42)

    def test_existing_menu_ticket_organization_and_assignment_survive_additive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.sqlite3'
            db=sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE,
                    created_at REAL NOT NULL, client_company TEXT NOT NULL DEFAULT '',
                    accepted_by INTEGER NOT NULL DEFAULT 0, accepted_at REAL NOT NULL DEFAULT 0);
                INSERT INTO tickets VALUES (1,100,1,'ТОВ "Раніше"',900,2);
                CREATE TABLE menu_messages (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, created_at REAL NOT NULL);
                INSERT INTO menu_messages VALUES (100,42,1);
            ''')
            db.close()
            store=Store(path)
            self.addCleanup(store.close)
            self.assertEqual(store.ticket(1)['client_company'],'ТОВ "Раніше"')
            self.assertEqual(store.ticket(1)['accepted_by'],900)
            self.assertEqual(store.menu_message(100)['message_id'],42)
            self.assertTrue(store.was_menu_message(100,42))
            self.assertEqual(store.ticket(1)['submission_id'],'')

    def test_registry_complete_and_all_content_has_metadata_and_placeholders(self):
        catalog=ScreenCatalog()
        expected={'M01','M02','R00','R50','R70','R80'}
        for prefix, start, end in (('D',1,4),('H',1,4),('F',0,3),('A',0,5),('B',0,4),('Q',0,8),('P',1,11),('E',1,3)):
            expected.update(f'{prefix}{n:02}' for n in range(start,end+1))
        self.assertEqual(set(catalog.screens),expected)
        self.assertTrue((ROOT/'docs/InfoBot_Content_Technical_v0_1.docx').exists())
        for screen in catalog.screens.values():
            self.assertIn('effective_from',screen)
            self.assertIn('legal_status',screen)
        with self.assertRaises(ValueError):
            catalog.text('P06')

    def test_privacy_configuration_validation_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text('{"operator_id":900}')
            self.assertEqual(Settings.load(path).privacy_policy_url,'')
            for value in ('http://example.test','javascript:bad','https://user:pass@example.test','https://example.test/\n'):
                path.write_text(json.dumps({'privacy_policy_url':value}))
                with self.assertRaises(ValueError):
                    Settings.load(path)
            settings=Settings(privacy_policy_url='https://example.test/privacy')
            settings.save(path)
            self.assertEqual(Settings.load(path).privacy_policy_url,settings.privacy_policy_url)
