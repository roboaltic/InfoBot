from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_document_content as flows
from infobot.engine import Engine
from infobot.faq import FAQCatalog, load_faq
from infobot.screens import ScreenCatalog
from infobot.settings import ROOT, Settings
from infobot.store import Store


class Revision02Flows(unittest.TestCase):
    # Спільний транспорт інтеграційних сценаріїв, без дублювання їхніх тестів.
    setUp=flows.DocumentFlowTests.setUp
    connect=flows.DocumentFlowTests.connect
    restart=flows.DocumentFlowTests.restart
    drain=flows.DocumentFlowTests.drain
    message=flows.DocumentFlowTests.message
    callback=flows.DocumentFlowTests.callback
    screen=flows.DocumentFlowTests.screen
    buttons=flows.DocumentFlowTests.buttons
    click=flows.DocumentFlowTests.click
    select=flows.DocumentFlowTests.select
    review=flows.DocumentFlowTests.review
    ticket=flows.DocumentFlowTests.ticket

    def test_personnel_return_repeat_and_notification_have_precise_norms(self):
        self.select()
        self.click('s:B00')
        self.click('s:B03')
        text=self.screen()['text']
        self.assertIn('Зарплатна модель також потребує щоквартального документального підтвердження',text)
        self.assertNotIn('абзаци 25–34 підпункту 18, підпункти 20 та 21 пункту 1',text)
        self.assertNotIn('з 24.06.2022',text)
        self.click('doc_sources:s:B03')
        self.assertTrue(any(b.get('url','').endswith('#n869') for b in self.buttons()))
        self.click('doc_sources_back')
        refs=self.engine.document_content.screens['B03']['references']
        self.assertTrue(any('абзаци 25–34' in r['description'] for r in refs))
        self.click('doc_back')
        self.click('s:B04')
        for sid in ('Q02','Q03'):
            self.click('s:'+sid)
            text=self.screen()['text']
            self.assertIn('через невиконання обов’язкових умов',text)
            self.assertIn('без права повторного отримання гранту',text)
            self.assertIn('не поширюється автоматично',text)
            self.assertIn('пункт 29',text.lower())
            self.click('doc_back')
        self.click('s:M02')
        self.click('s:A00')
        self.click('s:A05')
        self.assertIn('пункт 24 передбачає перенесення розгляду',self.screen()['text'])
        self.assertNotIn('пункти 24–25 — подальше оформлення',self.screen()['text'])
        self.click('s:M02')
        self.click('s:Q00')
        self.click('doc_page:1')
        self.click('s:Q08')
        text=self.screen()['text']
        self.assertIn('пункт 25',text)
        self.assertIn('пунктом 23',text)
        self.assertIn('SMS',text)
        self.assertIn('Пункт 24 стосується перенесення',text)

    def test_catalog_does_not_render_obsolete_warnings_or_internal_source_codes(self):
        self.select('D03','R80')
        catalog=self.engine.document_content
        for sid in catalog.screens:
            if sid.startswith(('P','E')):
                continue
            with self.subTest(sid=sid):
                self.engine.document_render(100,sid)
                self.drain()
                text=self.screen()['text']
                self.assertNotRegex(text,r'інформаційний вибір|потребує звірення|не завантаж|№813|\bS[1-8]\b|screen_id')
        self.assertNotIn('doc_legacy',[b.get('callback_data','').split(':',2)[-1] for b in self.buttons()])

    def test_helper_rejects_long_answer_at_current_step_and_never_classifies_territory(self):
        self.message('/start')
        self.click('s:D03')
        self.click('s:R00')
        self.assertIn('Для відновлення пошкоджених основних засобів передбачено',self.screen()['text'])
        self.assertIsNone(self.engine.nav_state(100)['ratio'])
        self.click('s:H01')
        self.message('Довільна адреса без підтвердженого статусу території')
        self.message('а'*501)
        self.assertIn('Оберіть сферу діяльності',self.screen()['text'])
        self.assertNotIn('У якому населеному пункті',self.screen()['text'])
        self.message('Інше виробництво, КВЕД')
        self.message('Українське обладнання')
        state=self.engine.nav_state(100)
        self.assertIsNone(state['ratio'])
        self.assertFalse(state['confirmed'])
        self.assertNotIn('eligible',state)
        self.assertEqual(len(state['help_answers']),3)

    def test_context_is_captured_on_submit_and_later_navigation_cannot_rewrite_ticket(self):
        self.select('D01','R50')
        self.click('s:F00')
        self.click('s:F01')
        self.click('support')
        self.review()
        self.select('D03','R80')
        self.click('support')
        self.assertIn('Відновлення виробництва',self.screen()['text'])
        self.click('form_send')
        ticket=self.store.user_ticket(100)
        saved=json.loads(ticket['grant_context'])
        self.assertEqual(saved['direction'],'restore')
        self.assertEqual(saved['ratio'],'80_20')
        self.assertEqual(saved['origin'],'s:F01')
        self.select('D02','R70')
        self.restart()
        self.assertEqual(json.loads(self.store.ticket(ticket['id'])['grant_context']),saved)

    def test_migrated_legacy_answer_requires_explicit_revision_approval(self):
        self.select()
        old=self.catalog.get('about')
        self.assertTrue(old['needs_revision_review'])
        self.callback('faq:about')
        self.assertNotIn(old['answer'],self.screen()['text'])
        self.message('/admin',900)
        self.callback('faqapprove:about:'+str(self.catalog.revision))
        self.assertTrue(self.catalog.get('about')['needs_revision_review'])
        self.callback('faqapprove:about:'+str(self.catalog.revision),900)
        self.assertFalse(self.catalog.get('about').get('needs_revision_review'))
        self.assertEqual(self.catalog.get('about')['content_revision'],'0.2')
        self.restart()
        self.engine.document_render(100,'Q00')
        self.drain()
        self.click('doc_legacy')
        self.click('faq:about')
        self.assertIn(old['answer'],self.screen()['text'])

    def test_queued_old_menu_is_regenerated_without_touching_menu_id_or_notifications(self):
        self.select()
        current_id=self.store.menu_message(100)['message_id']
        with self.store.db:
            state=self.engine.nav_state(100)
            state['screen']='s:B03'
            self.store.save_navigation(100,state)
            self.store.enqueue('sendMessage',{'chat_id':100,'text':'Старий текст 0.1',
                '_content_revision':'0.1','reply_markup':{'inline_keyboard':[[
                    {'text':'Назад','callback_data':f'n:{state["revision"]}:doc_back'}]]}},is_menu=True)
            self.store.enqueue('sendMessage',{'chat_id':100,'text':'Збережене повідомлення діалогу'})
        self.restart()
        self.assertEqual(self.store.menu_message(100)['message_id'],current_id)
        jobs=[json.loads(row[0]) for row in self.store.db.execute('SELECT payload FROM outbox ORDER BY id')]
        self.assertTrue(any(item['text']=='Збережене повідомлення діалогу' for item in jobs))
        self.assertFalse(any('Старий текст 0.1' in item['text'] for item in jobs))
        self.drain()
        self.assertEqual(self.store.menu_message(100)['message_id'],current_id)
        self.assertIn('Зарплатна модель також потребує щоквартального',self.screen()['text'])
        self.assertTrue(all('_content_revision' not in payload for _,payload in self.api.calls))

    def test_pending_field_input_keeps_values_when_menu_revision_is_refreshed(self):
        self.message('/ask')
        self.review()
        self.click('form_edit')
        self.click('form_field_phone')
        saved=dict(self.store.draft(100))
        with self.store.db:
            state=self.engine.nav_state(100)
            self.store.enqueue('sendMessage',{'chat_id':100,'text':'Старий запит телефону',
                '_content_revision':'0.1','reply_markup':{'inline_keyboard':[[
                    {'text':'Назад','callback_data':f'n:{state["revision"]}:form_back'}]]}},
                draft_user_id=100,is_menu=True)
        self.restart()
        self.drain()
        self.assertEqual(self.store.draft(100)['submission_id'],saved['submission_id'])
        self.assertEqual(self.store.draft(100)['client_email'],saved['client_email'])
        self.assertEqual(self.store.draft(100)['editing_field'],'phone')
        self.message('+380991234567')
        self.assertEqual(self.store.draft(100)['step'],'review')
        self.assertIsNone(self.store.user_ticket(100))

    def test_new_faq_from_admin_can_be_reviewed_and_published_in_current_catalog(self):
        self.select()
        self.message('/admin',900)
        self.callback('faqadd',900)
        self.message('Нове запитання поточної редакції?',900)
        self.message('Нова відповідь спеціаліста.',900)
        self.message('https://example.test/source',900)
        item=next(item for item in self.catalog.snapshot()[0] if item['question']=='Нове запитання поточної редакції?')
        self.assertTrue(item['needs_revision_review'])
        self.assertNotIn(item,self.engine.visible_legacy_faq(100))
        self.callback('faqapprove:'+item['id']+':'+str(self.catalog.revision),900)
        self.restart()
        self.click('s:Q00')
        self.click('doc_legacy')
        self.click('faq:'+item['id'])
        self.assertIn('Нова відповідь спеціаліста.',self.screen()['text'])


class Revision02MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'faq_catalog.json'
        self.current=ScreenCatalog()
        self.old=load_faq(ROOT/'content/faq_v01.json')

    def test_typical_content_updates_custom_publication_and_pending_draft_are_preserved(self):
        catalog=FAQCatalog(load_faq(ROOT/'faq.json'),self.path)
        catalog.seed(self.old,'content_v01')
        catalog.update('q03',{'answer':'Авторська опублікована відповідь'},catalog.revision)
        catalog.approve('q03',catalog.revision)
        catalog.update('q03',{'answer':'Незавершена авторська правка'},catalog.revision)
        catalog.delete('q07',catalog.revision)
        before_legacy=catalog.get('about')['answer']
        catalog.upgrade_content(self.current.faq_defaults,self.old)
        expected=next(item for item in self.current.faq_defaults if item['id']=='q03')
        self.assertEqual(catalog.get('q03')['answer'],expected['answer'])
        self.assertEqual(catalog.get('q03')['previous_publication']['answer'],'Авторська опублікована відповідь')
        self.assertEqual(catalog.preview('q03')['answer'],'Незавершена авторська правка')
        self.assertIn('№1326',catalog.get('q02')['source_points'])
        self.assertIsNone(catalog.get('q07'))
        self.assertEqual(catalog.get('about')['answer'],before_legacy)
        self.assertTrue(catalog.get('about')['needs_revision_review'])
        revision=catalog.revision
        catalog=FAQCatalog(load_faq(ROOT/'faq.json'),self.path)
        catalog.upgrade_content(self.current.faq_defaults,self.old)
        self.assertEqual(catalog.revision,revision)
        self.assertIsNone(catalog.get('q07'))

    def test_full_legacy_catalog_gets_all_eight_cards_without_losing_any_record(self):
        items=[{'id':'legacy_'+str(i),'question':'Запитання '+str(i),'answer':'Авторська відповідь '+str(i)} for i in range(60)]
        catalog=FAQCatalog(items,self.path)
        catalog.upgrade_content(self.current.faq_defaults,self.old)
        self.assertEqual(len(catalog.snapshot()[0]),68)
        for item in items:
            self.assertEqual(catalog.get(item['id'])['answer'],item['answer'])
        self.assertTrue(all(catalog.get('q'+f'{n:02}') for n in range(1,9)))

    def test_failed_migration_write_leaves_disk_and_memory_unchanged_for_retry(self):
        catalog=FAQCatalog(load_faq(ROOT/'faq.json'),self.path)
        catalog.seed(self.old,'content_v01')
        before=self.path.read_bytes()
        snapshot=catalog.snapshot()
        with patch('infobot.faq.atomic_write',side_effect=OSError('тестова помилка')):
            with self.assertRaises(OSError):
                catalog.upgrade_content(self.current.faq_defaults,self.old)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(catalog.snapshot(),snapshot)
        catalog.upgrade_content(self.current.faq_defaults,self.old)
        self.assertIn('content_v02',json.loads(self.path.read_text())['migrations'])

    def test_revision_metadata_and_supplements_are_separate_from_client_copy(self):
        self.assertEqual(len(self.current.screens),52)
        self.assertEqual(self.current.data['revision'],'0.2')
        self.assertEqual(self.current.data['sources']['S7']['effective_from'],'2026-10-27')
        self.assertNotIn('не завантаж',json.dumps(self.current.data,ensure_ascii=False))
        self.assertNotIn('№813',json.dumps(self.current.data,ensure_ascii=False))
        for sid,screen in self.current.screens.items():
            self.assertEqual(screen['checked_at'],'2026-11-11')
            for ref in screen['references']:
                self.assertTrue(ref['url'].startswith('https://'))
                self.assertTrue(ref['description'])
                if ref['source_id']=='S1':
                    self.assertTrue(ref['point'],sid)
