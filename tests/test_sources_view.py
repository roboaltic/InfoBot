"""Source navigation uses the production engine with a fake Telegram and temporary DB."""
import copy
import json
import unittest

from infobot.screens import ScreenCatalog
import test_document_content as document_flows


class SourcesViewFlows(unittest.TestCase):
    setUp = document_flows.DocumentFlowTests.setUp
    connect = document_flows.DocumentFlowTests.connect
    restart = document_flows.DocumentFlowTests.restart
    drain = document_flows.DocumentFlowTests.drain
    message = document_flows.DocumentFlowTests.message
    callback = document_flows.DocumentFlowTests.callback
    screen = document_flows.DocumentFlowTests.screen
    buttons = document_flows.DocumentFlowTests.buttons
    click = document_flows.DocumentFlowTests.click
    select = document_flows.DocumentFlowTests.select
    review = document_flows.DocumentFlowTests.review

    NOTICE = 'Більше інформації — за кнопкою «Джерела».'

    def links(self, uid=100):
        return [(b['text'], b['url']) for b in self.buttons(uid) if 'url' in b]

    def test_one_source_in_same_static_menu_and_exact_footer(self):
        self.select()
        self.click('s:F00')
        self.click('s:F02')
        answer = copy.deepcopy(self.screen())
        menu = self.store.menu_message(100)['message_id']
        self.assertEqual(answer['text'].count(self.NOTICE), 1)
        self.assertTrue(answer['text'].endswith(self.NOTICE))
        self.assertFalse(self.links())
        self.assertEqual(sum(b['text'] == 'Джерела' for b in self.buttons()), 1)
        self.click('doc_sources:s:F02')
        self.assertEqual(self.screen()['text'], 'Документи до цієї відповіді\nОберіть документ, щоб перейти до відповідного положення.')
        self.assertEqual(self.links(), [('Порядок надання грантів № 739 · п. 4',
                         'https://zakon.rada.gov.ua/laws/show/739-2022-%D0%BF#n630')])
        self.assertEqual(menu, self.store.menu_message(100)['message_id'])
        self.assertNotIn(self.NOTICE, self.screen()['text'])
        self.click('doc_sources_back')
        self.assertEqual(self.screen()['text'], answer['text'])
        self.assertEqual(menu, self.store.menu_message(100)['message_id'])

    def test_multiple_points_no_duplicates_or_automatic_amendments(self):
        self.select()
        self.click('s:B00')
        self.click('s:B02')
        self.click('doc_sources:s:B02')
        links = self.links()
        self.assertEqual([label for label, _ in links], [f'Порядок надання грантів № 739 · п. {n}' for n in (6,33,39)])
        self.assertEqual(len(links), len({url for _, url in links}))
        self.assertFalse(any('1326' in label for label,_ in links))
        self.assertFalse(any(label.startswith('Джерело') for label,_ in links))

    def test_multiple_documents_faq_and_return_keep_page_and_selection(self):
        self.select('D03','R80')
        self.click('s:Q00')
        self.click('s:Q01')
        state = self.engine.nav_state(100)
        self.click('doc_sources:s:Q01')
        self.assertEqual([label for label,_ in self.links()],
                         ['Мінекономіки · оновлення програми','Дія · графік приймання заяв'])
        self.click('doc_sources_back')
        self.assertEqual(self.engine.nav_state(100)['screen'], 's:Q01')
        for key in ('direction','ratio','confirmed','faq_page','history'):
            self.assertEqual(self.engine.nav_state(100)[key], state[key])
        self.click('doc_back')
        self.click('doc_page:1')
        self.click('s:Q08')
        state = self.engine.nav_state(100)
        self.click('doc_sources:s:Q08')
        self.click('doc_sources_back')
        self.assertEqual(self.engine.nav_state(100)['screen'], 's:Q08')
        self.assertEqual(self.engine.nav_state(100)['faq_page'], 1)
        self.assertEqual(self.engine.nav_state(100)['history'], state['history'])
        self.click('doc_back')
        self.assertEqual(self.engine.nav_state(100)['screen'], 's:Q00')
        self.assertEqual(self.engine.nav_state(100)['faq_page'], 1)

    def test_menus_greeting_helpers_forms_and_service_messages_have_no_source_footer(self):
        self.select()
        for sid in ('M01','M02','D01','D02','D03','D04','R00','H01','H02','H03','H04','F00','A00','B00','Q00'):
            with self.subTest(sid=sid), self.store.db:
                self.engine.document_render(100,sid)
                self.drain()
                self.assertNotIn(self.NOTICE,self.screen()['text'])
                self.assertFalse(any(b['text']=='Джерела' for b in self.buttons()))
                self.assertFalse(self.links())
        self.message('/ask')
        self.assertNotIn(self.NOTICE,self.screen()['text'])
        self.assertFalse(any(b['text']=='Джерела' for b in self.buttons()))
        self.message('/admin',900)
        self.assertNotIn(self.NOTICE,self.screen(900)['text'])

    def test_answer_without_sources_has_neither_footer_nor_button(self):
        self.select()
        screen = self.engine.document_content.screens['F02']
        screen['references'] = []
        screen['source_ids'] = []
        self.click('s:F00')
        self.click('s:F02')
        self.assertNotIn(self.NOTICE,self.screen()['text'])
        self.assertFalse(self.links())
        self.assertFalse(any(b['text']=='Джерела' for b in self.buttons()))
        state = self.engine.nav_state(100)
        self.callback(f"n:{state['revision']}:doc_sources:s:F02")
        self.assertEqual(self.engine.nav_state(100),state)

    def test_footer_is_not_accumulated_across_reopens_or_authored_copy(self):
        self.select()
        self.engine.document_content.screens['F02']['body'] += '\n\n'+self.NOTICE+'\n'+self.NOTICE
        self.click('s:F00')
        self.click('s:F02')
        for _ in range(3):
            self.assertEqual(self.screen()['text'].count(self.NOTICE),1)
            self.click('doc_sources:s:F02')
            self.click('doc_sources_back')
        self.assertEqual(self.screen()['text'].count(self.NOTICE),1)

    def test_two_users_have_independent_source_screens_and_context(self):
        self.select('D01','R50',100)
        self.select('D03','R80',200)
        self.click('s:F00',100)
        self.click('s:F02',100)
        self.click('s:B00',200)
        self.click('s:B02',200)
        before = copy.deepcopy(self.engine.nav_state(200))
        self.click('doc_sources:s:F02',100)
        self.assertEqual(len(self.links(100)),1)
        self.assertEqual(self.engine.nav_state(200),before)
        self.click('doc_sources:s:B02',200)
        self.assertEqual(len(self.links(200)),3)
        self.assertEqual(len(self.links(100)),1)
        self.click('doc_sources_back',100)
        self.assertEqual(self.engine.nav_state(100)['ratio'],'50_50')
        self.assertEqual(self.engine.nav_state(200)['screen'],'sources:s:B02')
        self.click('doc_sources_back',200)
        self.assertEqual(self.engine.nav_state(200)['ratio'],'80_20')

    def test_old_open_and_back_buttons_cannot_change_current_answer(self):
        self.select()
        self.click('s:F00')
        self.click('s:F02')
        old_open = next(b['callback_data'] for b in self.buttons() if b['text']=='Джерела')
        self.click('doc_sources:s:F02')
        old_back = next(b['callback_data'] for b in self.buttons() if b['text']=='Назад до відповіді')
        self.click('doc_sources_back')
        self.click('s:M02')
        self.click('s:B00')
        self.click('s:B02')
        state = copy.deepcopy(self.engine.nav_state(100))
        text = self.screen()['text']
        self.callback(old_open)
        self.callback(old_back)
        self.assertEqual(self.engine.nav_state(100),state)
        self.assertEqual(self.screen()['text'],text)
        self.callback(f"n:{state['revision']}:doc_sources:s:F02")
        self.assertEqual(self.engine.nav_state(100),state)
        self.assertTrue(any(method=='answerCallbackQuery' and 'застаріла' in p.get('text','')
                            for method,p in self.api.calls))

    def test_source_screen_survives_restart_and_cache_presentation_refresh(self):
        self.select()
        self.click('s:Q00')
        self.click('doc_page:1')
        self.click('s:Q08')
        with self.store.db:
            self.engine.document_sources(100,'s:Q08')
            row=self.store.db.execute('SELECT * FROM outbox WHERE is_menu=1 AND destination=100 ORDER BY id DESC').fetchone()
            payload=json.loads(row['payload'])
            payload['_presentation_revision']='old'
            self.store.db.execute('UPDATE outbox SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
        self.restart()
        self.drain()
        self.assertEqual(self.engine.nav_state(100)['screen'],'sources:s:Q08')
        self.assertEqual(len(self.links()),3)
        self.click('doc_sources_back')
        self.assertEqual(self.engine.nav_state(100)['screen'],'s:Q08')
        self.assertEqual(self.engine.nav_state(100)['faq_page'],1)

    def test_custom_published_faq_uses_the_same_source_resolver(self):
        self.select()
        item = dict(id='custom_sources', question='Яка форма документа?', answer='Використайте форму наказу.',
                    source_url=self.engine.document_content.data['sources']['S5']['url'],
                    content_revision='0.2', directions=['*'], ratios=['*'])
        self.catalog.add(item, self.catalog.revision)
        self.catalog.approve(item['id'], self.catalog.revision)
        with self.store.db:
            self.engine.show_faq_answer(100,item['id'])
        self.drain()
        before = self.screen()['text']
        self.assertEqual(before.count(self.NOTICE),1)
        self.assertFalse(self.links())
        self.click('doc_sources:legacy_answer:custom_sources')
        self.assertEqual(self.links(), [('Наказ Мінекономіки № 2109', item['source_url'])])
        self.click('doc_sources_back')
        self.assertEqual(self.screen()['text'], before)


class SourceResolverTests(unittest.TestCase):
    def test_titles_dedup_and_explicit_amendment_reference(self):
        catalog=ScreenCatalog()
        item={'sources':[catalog.data['sources'][key]['url'] for key in ('S5','S5','S7')]}
        self.assertEqual(catalog.answer_sources(item=item),
            [('Наказ Мінекономіки № 2109',catalog.data['sources']['S5']['url'])])
        item={'references':[dict(source_id='S7',point='',client_visible=True,
                                url=catalog.data['sources']['S7']['url'])]}
        self.assertEqual(catalog.answer_sources(item=item),
            [('Постанова КМУ № 1326',catalog.data['sources']['S7']['url'])])
