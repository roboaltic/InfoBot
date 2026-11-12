import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from infobot.engine import Engine
from infobot.faq import load_faq
from infobot.runner import Delivery, Runner
from infobot.screens import ScreenCatalog
from infobot.settings import ROOT, Settings
from infobot.store import Store
from infobot.telegram import APIError
from test_bot import FakeAPI


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'chats.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = Settings(operator_id=900)
        self.engine = Engine(self.store, self.settings, load_faq(ROOT/'faq.json'))
        with self.store.db:
            self.first = self.store.create_ticket(100, 'Олена')
            self.second = self.store.create_ticket(200, 'Іван')
            self.store.add_message(self.first, 'client', 'Перше питання')
            self.store.add_message(self.second, 'client', 'Друге питання')
        self.api = FakeAPI()
        self.delivery = Delivery(self.store, self.api, lambda *args: None)

    def control(self, action, tid=None, **values):
        Runner.chat_control(SimpleNamespace(settings=self.settings), self.engine, self.store,
                            dict(action=action, operator_id=900, ticket_id=tid or self.first, **values))

    def flush(self):
        for _ in range(20):
            if not self.store.ready_jobs(time.time()):
                return
            self.delivery.sent_at.clear()
            self.delivery.flush()
        self.fail('Queue did not drain')

    def test_drafts_unread_and_full_history_survive_restart(self):
        self.control('draft', text='Чернетка А')
        self.control('draft', self.second, text='Чернетка Б')
        self.control('read')
        with self.store.db:
            self.store.add_message(self.first, 'client', 'Повідомлення після згортання')
        self.store.close()
        self.store = Store(self.path)
        rows = {r['id']:r for r in self.store.chat_snapshot()}
        self.assertEqual((rows[self.first]['unread'], rows[self.second]['unread']), (1,1))
        self.assertEqual(rows[self.first]['draft'], 'Чернетка А')
        self.assertEqual(rows[self.second]['draft'], 'Чернетка Б')
        self.assertEqual(len(rows[self.first]['history']), 2)

    def test_success_only_after_transport_and_immutable_recipient(self):
        self.control('send', text='Відповідь А')
        self.control('read', self.second)
        self.control('draft', self.second, text='Відповідь Б')
        self.assertEqual(len(self.store.history(self.first, -1)), 1)
        self.assertFalse(any('Повідомлення надіслано клієнту.' in p['text'] for _,p in self.api.calls))
        self.flush()
        replies = [(p['chat_id'],p['text']) for method,p in self.api.calls if method=='sendMessage']
        self.assertTrue(any(uid==100 and 'Відповідь А' in body for uid,body in replies))
        self.assertFalse(any(uid==200 for uid,_ in replies))
        self.assertTrue(any(uid==900 and 'Повідомлення надіслано клієнту.' in body for uid,body in replies))
        self.assertEqual(len(self.store.history(self.first,-1)),2)
        self.assertEqual(self.store.chat_snapshot()[0]['draft'],'Відповідь Б')

    def test_double_send_is_blocked(self):
        self.control('send', text='Одна відповідь')
        self.control('send', text='Одна відповідь')
        self.flush()
        self.assertEqual(sum(method=='sendMessage' and p['chat_id']==100 for method,p in self.api.calls),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM specialist_replies').fetchone()[0],1)

    def test_failed_delivery_retains_text_explicit_retry(self):
        self.control('send', text='Збережений текст')
        self.api.error = APIError(403, description='Forbidden')
        self.delivery.flush()
        row = self.store.chat_snapshot()[-1]
        self.assertEqual(row['draft'], 'Збережений текст')
        self.assertEqual(row['replies'][-1]['status'],'failed')
        self.assertEqual(len(row['history']),1)
        self.api.error = None
        self.control('retry', reply_id=row['replies'][-1]['id'])
        self.flush()
        self.assertEqual(len(self.store.history(self.first,-1)),2)
        self.assertEqual(self.store.chat_snapshot()[-1]['draft'],'')

    def test_unknown_delivery_never_automatically_retries(self):
        for code in (0,500):
            with self.subTest(code=code):
                self.api.calls.clear()
                self.control('send', text=f'Невизначений результат {code}')
                self.api.error = APIError(code,description='network failure')
                self.delivery.flush()
                row = self.store.chat_snapshot()[-1]
                self.assertEqual(row['replies'][-1]['status'],'unknown')
                self.api.error = None
                self.flush()
                self.assertEqual(sum(p.get('chat_id')==100 for _,p in self.api.calls),1)
                self.assertFalse(any('Повідомлення надіслано клієнту.' in p.get('text','') for _,p in self.api.calls))
                with self.store.db:
                    self.store.db.execute("UPDATE specialist_replies SET status='cancelled'")

    def test_crash_during_transport_requires_manual_reconciliation(self):
        self.control('send', text='Перед перезапуском')
        with self.store.db:
            self.store.db.execute("UPDATE specialist_replies SET status='sending'")
        self.store.close()
        self.store = Store(self.path)
        self.delivery = Delivery(self.store,self.api,lambda *args:None)
        self.flush()
        self.assertEqual(self.store.chat_snapshot()[-1]['replies'][-1]['status'],'unknown')
        self.assertFalse(any(p.get('chat_id') == 100 for _, p in self.api.calls))

    def test_rights_and_explicit_completion(self):
        with self.assertRaises(ValueError):
            Runner.chat_control(SimpleNamespace(settings=self.settings),self.engine,self.store,
                dict(action='send',operator_id=123,ticket_id=self.first,text='Заборонено'))
        self.control('draft',text='Не завершувати')
        self.control('read',self.second)
        self.assertIsNotNone(self.store.ticket(self.first))
        self.control('close')
        self.assertIsNone(self.store.ticket(self.first))
        self.assertIsNotNone(self.store.ticket(self.second))

    def test_new_draft_is_not_erased_by_old_delivery(self):
        self.control('send', text='Надіслати')
        self.control('draft', text='Наступна чернетка')
        self.flush()
        self.assertEqual(self.store.chat_snapshot()[-1]['draft'],'Наступна чернетка')

    def test_read_acknowledges_only_messages_already_displayed(self):
        seen = self.store.history(self.first, -1)[-1]['id']
        with self.store.db:
            self.store.add_message(self.first, 'client', 'Нове повідомлення перед обробкою команди')
        self.control('read', read_id=seen)
        self.assertEqual(self.store.chat_snapshot()[-1]['unread'], 1)

    def test_unknown_result_can_be_retried_only_by_explicit_action(self):
        self.control('send', text='Повтор після перевірки')
        self.api.error = APIError(0, description='timeout')
        self.delivery.flush()
        row = self.store.chat_snapshot()[-1]['replies'][-1]
        self.api.error = None
        self.control('retry', reply_id=row['id'])
        self.control('retry', reply_id=row['id'])
        self.flush()
        self.assertEqual(sum(p.get('chat_id') == 100 for _, p in self.api.calls), 2)
        self.assertEqual(len(self.store.history(self.first, -1)), 2)

    def test_invalid_text_never_reaches_transport(self):
        for text in ('', '🙂'*1001):
            self.control('send',text=text)
        self.flush()
        self.assertFalse(any(p.get('chat_id')==100 for _,p in self.api.calls))


class VerifiedSourcesTests(unittest.TestCase):
    def test_verified_point_registry_and_unchanged_grant_content(self):
        import hashlib
        catalog=ScreenCatalog()
        bodies = {sid: screen.get('body', '') for sid, screen in catalog.screens.items()}
        self.assertEqual(hashlib.sha256(json.dumps(bodies, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                         '6e3d65cfd8f6c8da2faf4d7edde0c95f93a105e388b0e9e382ac763f45f7fd2f')
        for sid,screen in catalog.screens.items():
            for ref in screen.get('references',[]):
                if ref['source_id']=='S1':
                    self.assertTrue(ref['verified_links'])
                    for link in ref['verified_links']:
                        self.assertEqual(link['url'],catalog.data['point_links'][link['point']]['url'])
        for point,anchor in [('4','n630'),('26','n869'),('39','n986')]:
            self.assertTrue(catalog.data['point_links'][point]['url'].endswith('#'+anchor))
        evidence = json.loads((ROOT/'tests/fixtures/law_739_anchors.json').read_text())
        self.assertEqual(len(evidence['points']), len(catalog.data['point_links']))
        for entry in evidence['points']:
            link = catalog.data['point_links'][entry['point']]
            self.assertTrue(link['url'].endswith('#'+entry['anchor']))
            self.assertEqual(link['html_sha256'], evidence['html_sha256'])


class SourcePresentationFlows(unittest.TestCase):
    from test_document_content import DocumentFlowTests as _flows
    setUp = _flows.setUp
    connect = _flows.connect
    restart = _flows.restart
    drain = _flows.drain
    message = _flows.message
    callback = _flows.callback
    screen = _flows.screen
    buttons = _flows.buttons
    click = _flows.click
    select = _flows.select

    def test_cached_menu_with_same_legal_revision_is_reformatted_on_restart(self):
        self.select()
        with self.store.db:
            self.engine.document_render(100, 'F02')
            job = self.store.db.execute('SELECT id,payload FROM outbox WHERE destination=100 AND is_menu=1 ORDER BY id DESC').fetchone()
            payload = json.loads(job['payload'])
            payload.pop('_presentation_revision')
            payload['text'] += '\nСтарий довгий технічний підпис джерела'
            self.store.db.execute('UPDATE outbox SET payload=? WHERE id=?', (json.dumps(payload),job['id']))
        self.restart()
        self.drain()
        self.assertNotIn('Старий довгий технічний підпис',self.screen()['text'])
        self.assertTrue(any(b.get('url','').endswith('#n630') for b in self.buttons()))

    def test_multiple_norms_have_separate_short_sources(self):
        self.select()
        self.click('s:B00')
        self.click('s:B02')
        links = {b['text']:b['url'] for b in self.buttons() if 'url' in b}
        self.assertTrue(links['Джерело · пункт 6'].endswith('#n650'))
        self.assertTrue(links['Джерело · пункт 33'].endswith('#n944'))
        self.assertTrue(links['Джерело · пункт 39'].endswith('#n986'))
        self.assertNotIn('24.06.2022', self.screen()['text'])
