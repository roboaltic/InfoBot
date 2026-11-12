"""Спільний каталог екранів, нормативних посилань і переходів InfoBot."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from string import Formatter
from urllib.parse import urlsplit

from .faq import size, validate_source, validate_items
from .settings import ROOT

PRESENTATION_REVISION = 'sources-view-2026-11-12'

DIRECTIONS = {'D01': 'create', 'D02': 'develop', 'D03': 'restore'}
RATIOS = {'R50': '50_50', 'R70': '70_30', 'R80': '80_20'}
OLD_ROUTES = {'home': 'M01', 'choose_help': 'D04', 'ratios': 'R00', 'ratio_help': 'H01',
              'grant': 'M02', 'section:conditions': 'F00', 'section:application': 'A00',
              'section:after': 'B00', 'answer:eligibility': 'F01', 'answer:amount': 'F02',
              'answer:expenses': 'F03', 'answer:documents': 'A01', 'answer:submit': 'A03',
              'answer:checks': 'A04', 'answer:decision': 'A05', 'answer:contract': 'B01',
              'answer:equipment': 'B02', 'answer:jobs': 'B03', 'answer:completion': 'B04'}


class ScreenCatalog:
    def __init__(self, path: Path = ROOT / 'content' / 'screens.json'):
        self.data = json.loads(path.read_text(encoding='utf-8'))
        self.screens = self.data['screens']
        self.faq_defaults = self.load_faq_file(path, self.data.get('faq_file','faq_v01.json'))
        self.previous_faq_defaults = self.load_faq_file(path,self.data['previous_faq_file']) if self.data.get('previous_faq_file') else []
        self.validate()

    @staticmethod
    def load_faq_file(path, filename):
        if not isinstance(filename,str) or Path(filename).name!=filename or not filename.endswith('.json'):
            raise ValueError('Некоректне ім’я файла FAQ.')
        return validate_items(json.loads(path.with_name(filename).read_text(encoding='utf-8')))

    def validate(self):
        if self.data.get('schema') != 1 or self.data.get('faq_page_size') != 4:
            raise ValueError('Некоректний каталог екранів.')
        for source in self.data['sources'].values():
            validate_source(source['url'])
        for sid, screen in self.screens.items():
            if not re.fullmatch(r'[MDRHFABQPE][0-9]{2}', sid) or screen.get('screen_id') != sid:
                raise ValueError('Некоректний ідентифікатор екрана.')
            if not screen.get('revision') or not screen.get('approval_status'):
                raise ValueError('Екран потребує редакції та статусу погодження.')
            if not isinstance(screen['title'], str) or not 1 <= size(screen['title']) <= 80:
                raise ValueError('Некоректний заголовок екрана.')
            if not isinstance(screen.get('body', ''), str) or size(screen.get('body', '')) > 3000:
                raise ValueError('Завеликий текст екрана.')
            permitted = {'M02': {'напрям','співфінансування'},
                         'P06': {'напрям або «Не обрано»','співвідношення або «Не обрано»','тема','запитання',
                                 'ПІБ','організація або «Бізнес ще не зареєстровано»','телефон','email'},
                         'P08': {'Ім’я','номер'}, 'P09': {'номер'}, 'P10': {'Ім’я','номер'}}.get(sid,set())
            for _, field, spec, conversion in Formatter().parse(screen.get('body','')):
                if field and (field not in permitted or spec or conversion):
                    raise ValueError('Невідома підстановка екрана ' + sid)
            if screen.get('faq_id') and not any(item['id']==screen['faq_id'] and item.get('screen_id')==sid for item in self.faq_defaults):
                raise ValueError('Невідоме FAQ екрана.')
            if any(source not in self.data['sources'] for source in screen['source_ids']):
                raise ValueError('Невідоме джерело.')
            for ref in screen.get('references',[]):
                self.validate_reference(ref)
            for button in screen['buttons']:
                if not 1 <= size(button['label']) <= 64:
                    raise ValueError('Завеликий підпис кнопки.')
                target = button['target']
                if button['kind'] == 'source':
                    if target not in self.data['sources'] or not self.data['sources'][target]['url']:
                        raise ValueError('Невідоме зовнішнє посилання.')
                elif button['kind'] == 'navigate' and target not in self.screens and target not in (
                        'D01/D02/D03', 'R50/R70/R80', 'попередній екран', 'попереднє меню',
                        'M02 або Обрати напрям → M01', 'M02 або M01', 'екран до відкриття підтримки',
                        'поточне поле'):
                    raise ValueError('Невідомий перехід: ' + target)
                elif button['kind'] not in ('source', 'navigate', 'input'):
                    raise ValueError('Невідома дія кнопки.')
        for item in self.faq_defaults:
            for ref in item.get('references',[]):
                self.validate_reference(ref)

    def validate_reference(self, ref):
        if (not isinstance(ref,dict) or ref.get('source_id') not in self.data['sources'] or
            not isinstance(ref.get('description'),str) or not ref['description'].strip() or not isinstance(ref.get('point'),str)):
            raise ValueError('Некоректний опис нормативного посилання.')
        if not ref.get('url') or validate_source(ref['url'])!=self.data['sources'][ref['source_id']]['url']:
            raise ValueError('Посилання не відповідає джерелу.')
        for link in ref.get('verified_links', []):
            registered = self.data.get('point_links', {}).get(link.get('point'))
            if (ref['source_id'] != 'S1' or not registered or link.get('url') != registered['url']
                    or not re.fullmatch(r'https://zakon\.rada\.gov\.ua/laws/show/739-2022-%D0%BF#n\d+', link['url'])):
                raise ValueError('Неперевірений якір нормативного посилання.')

    def text(self, sid, **values):
        body = self.screens[sid].get('body', '')
        # Telegram plain text: user input never becomes HTML or Markdown.
        fields = [field for _, field, _, _ in Formatter().parse(body) if field]
        if any(field not in values for field in fields):
            raise ValueError('Не вистачає підстановок екрана ' + sid)
        return body.format_map(values)

    def answer_sources(self, sid=None, item=None):
        """Resolve one answer from catalog bindings; never calculate HTML anchors."""
        screen = self.screens.get(sid, {})
        if item is None and not screen.get('sources_enabled'):
            return []
        record = item if item is not None else screen
        sources = self.data['sources']
        links = []
        for ref in record.get('references', []):
            source = sources.get(ref.get('source_id'))
            if not source or (source.get('client_role') == 'amendment_metadata' and not ref.get('client_visible')):
                continue
            title = source.get('client_title', source['short_title'])
            if ref['source_id'] == 'S1' and ref.get('point'):
                for part in ref['point'].split(','):
                    numbers = [int(n) for n in re.findall(r'\d+', part)]
                    if not numbers:
                        continue
                    for point in range(numbers[0], numbers[-1] + 1):
                        verified = self.data.get('point_links', {}).get(str(point))
                        if verified:
                            links.append((title + ' · п. ' + str(point), verified['url']))
            else:
                links.append((title, ref['url']))
        if not record.get('references'):
            urls = (record.get('sources', [record['source_url']] if record.get('source_url') else [])
                    if item is not None else [sources[key]['url'] for key in screen.get('source_ids', [])])
            for url in urls:
                if not url:
                    continue
                source = next((s for s in sources.values() if s['url'] == url), None)
                if source and source.get('client_role') == 'amendment_metadata':
                    continue
                title = (source.get('client_title', source['short_title']) if source else
                         record.get('source_title') or urlsplit(url).hostname)
                links.append((title, url))
        # One URL is one button, even if multiple bindings refer to it.
        return list({url: (title, url) for title, url in links}.values())


class DocumentNavigation:
    def document_route(self, uid, route):
        if route and route.startswith(('legacy_faq:', 'legacy_answer:', 'sources:')):
            return route
        if route and route.startswith('s:'):
            return route[2:]
        if route == 'preview':
            return next((sid for sid, ratio in RATIOS.items() if ratio == self.nav_state(uid)['ratio']), 'R00')
        return OLD_ROUTES.get(route, 'M01')

    def document_links(self, uid):
        ui = self.document_content.data['ui']
        state = self.nav_state(uid)
        rows = [[(ui['back'], 'doc_back')]]
        if state['confirmed']:
            rows.append([(ui['grant'], 's:M02')])
        rows.append([(ui['direction'], 'doc_direction')])
        if state['direction']:
            rows.append([(ui['ratio'], 'doc_ratio')])
        rows.append([(ui['support'], 'support')])
        return rows

    def document_faq(self, uid):
        state = self.nav_state(uid)
        return [item for item in self.faq if item.get('screen_id') and not item.get('needs_revision_review') and
                ('*' in item.get('directions', ['*']) or state['direction'] in item['directions']) and
                ('*' in item.get('ratios', ['*']) or state['ratio'] in item['ratios'])]

    def document_render(self, uid, sid, *, fresh_menu=False, remember=True):
        if sid.startswith('sources:'):
            return self.document_sources(uid, sid[len('sources:'):], fresh_menu=fresh_menu)
        if sid.startswith('legacy_faq:'):
            return self.show_faq(uid,sid.partition(':')[2])
        if sid.startswith('legacy_answer:'):
            return self.show_faq_answer(uid,sid.partition(':')[2])
        catalog = self.document_content
        state = self.nav_state(uid)
        if sid not in catalog.screens:
            sid = 'M02' if state['confirmed'] else 'M01'
        if sid in ('R00', 'R50', 'R70', 'R80', 'H01', 'H02', 'H03', 'H04') and not state['direction']:
            sid = 'M01'
        if sid == 'M02' and not state['confirmed']:
            sid = 'R00' if state['direction'] else 'M01'
        old = self.document_route(uid, state['screen'])
        if old.startswith('sources:'):
            old = self.document_route(uid, old[len('sources:'):])
        if remember and old != sid:
            state['history'] = (state['history'] + [old])[-32:]
        state.update(screen='s:' + sid, updated_at=time.time(), helper_input=sid in ('H01','H02','H03'))
        self.store.save_navigation(uid, state)
        screen = catalog.screens[sid]
        ui = catalog.data['ui']
        title = screen['title']
        text = catalog.text(sid, **{'напрям': self.content.data['directions'].get(state['direction'], ui['no_direction']),
                                  'співфінансування': (state['ratio'] or '').replace('_', '/')})
        rows = []
        if sid.startswith('Q') and sid != 'Q00':
            item = self.faq_catalog.get(screen['faq_id'])
            if not item or item not in self.document_faq(uid):
                return self.document_render(uid, 'Q00')
            title, text = item['question'], item['answer']
        if sid == 'Q00':
            items = self.document_faq(uid)
            page = min(state['faq_page'], max(0, (len(items)-1)//4))
            state['faq_page'] = page
            self.store.save_navigation(uid, state)
            rows = [[(item['question'], 's:' + item['screen_id'])] for item in items[page*4:(page+1)*4]]
            if page:
                rows.append([(ui['previous'], 'doc_page:' + str(page-1))])
            if (page+1)*4 < len(items):
                rows.append([(ui['next'], 'doc_page:' + str(page+1))])
            if self.visible_legacy_faq(uid):
                rows.append([(ui['legacy_faq'], 'doc_legacy')])
        else:
            for index, button in enumerate(screen['buttons']):
                label, target, kind = button['label'], button['target'], button['kind']
                if label == ui['back'] or sid.startswith('P') or sid.startswith('E'):
                    continue
                if kind == 'source':
                    continue
                if sid in ('H02', 'H03'):
                    if target.startswith('H'):
                        route = f'doc_help:{sid}:{index}'
                    else:
                        continue
                elif kind == 'input':
                    route = 'doc_input:' + sid
                elif sid == 'H01' and target == 'H02':
                    route = f'doc_help:{sid}:{index}'
                elif target == 'P01':
                    route = 'support'
                elif target == 'M01' and sid == 'M02':
                    route = 'doc_direction'
                elif target == 'R00' and sid == 'M02':
                    route = 'doc_ratio'
                elif target == 'R50/R70/R80':
                    route = 's:' + next((key for key,value in RATIOS.items() if value == state['ratio']), 'R00')
                else:
                    route = 's:' + target
                rows.append([(label, route)])
        if sid not in ('M01', 'M02', 'D04'):
            existing = {route for row in rows for _,route in row}
            rows += [row for row in self.document_links(uid) if row[0][1] not in existing]
        elif sid == 'D04':
            rows.append([(ui['back'], 'doc_back')])
        if uid == self.settings.operator_id and sid == 'M01':
            rows.append([(self.nav_label('admin'), 'admin')])
        if sid in RATIOS and catalog.data.get('selection_notice'):
            text += '\n\n' + catalog.data['selection_notice']
        if catalog.data.get('legal_notice') and sid.startswith(('F', 'A', 'B', 'Q', 'R')) and sid not in ('Q00', 'R00'):
            text += '\n\n' + catalog.data['legal_notice']
        if sid == 'H04':
            text += '\n\n' + ui['helper_summary'] + '\n' + '\n'.join(state['help_answers'].values())
            text += '\n\n' + ui['helper_notice']
        if sid=='R00' and state['direction']=='restore':
            text += '\n\n' + catalog.data.get('recovery_notice','')
        if sid not in ('M01', 'D04') and state['direction']:
            title = self.grant_title(uid) + '\n\n' + title
        item = item if sid.startswith('Q') and sid != 'Q00' else None
        links = catalog.answer_sources(sid, item)
        body = title + '\n\n' + text
        body, markup = self.answer_source_button(uid, 's:' + sid, body, rows, links)
        self.say(uid, body, markup, fresh_menu=fresh_menu)

    def answer_source_button(self, uid, origin, text, rows, links):
        view = self.document_content.data['sources_view']
        # Re-render from content, with one footer even if already present in authored text.
        text = text.replace(view['notice'], '').rstrip()
        if links:
            text += '\n\n' + view['notice']
            rows = [[(view['button'], 'doc_sources:' + origin)]] + rows
        return text, self.nav_markup(uid, rows)

    def sources_for_answer(self, uid, origin):
        catalog = self.document_content
        if origin.startswith('legacy_answer:'):
            item = self.faq_catalog.get(origin.partition(':')[2])
            return catalog.answer_sources(item=item) if item and item in self.visible_legacy_faq(uid) else []
        sid = origin[2:] if origin.startswith('s:') else ''
        screen = catalog.screens.get(sid, {})
        if not screen.get('sources_enabled'):
            return []
        if screen.get('faq_id'):
            item = self.faq_catalog.get(screen['faq_id'])
            return catalog.answer_sources(sid, item) if item and item in self.document_faq(uid) else []
        return catalog.answer_sources(sid)

    def document_sources(self, uid, origin, *, fresh_menu=False):
        links = self.sources_for_answer(uid, origin)
        if not links:
            return self.document_render(uid, self.document_route(uid, origin), remember=False)
        state = self.nav_state(uid)
        state.update(screen='sources:' + origin, helper_input=False, updated_at=time.time())
        self.store.save_navigation(uid, state)
        view = self.document_content.data['sources_view']
        markup = self.nav_markup(uid, [[(view['back'], 'doc_sources_back')]])
        markup['inline_keyboard'][:0] = [[{'text': label, 'url': url}] for label, url in links]
        self.say(uid, view['title'] + '\n' + view['body'], markup, fresh_menu=fresh_menu)

    def refresh_queued_content(self):
        """Не доставляти закешований текст старої редакції після перезапуску."""
        jobs=self.store.db.execute('SELECT * FROM outbox WHERE is_menu=1 ORDER BY id').fetchall()
        latest={job['destination']:job for job in jobs}
        for uid,job in latest.items():
            if uid==self.settings.operator_id:
                continue
            payload=json.loads(job['payload'])
            if (payload.get('_content_revision')==self.document_content.data['revision']
                    and payload.get('_presentation_revision') == PRESENTATION_REVISION):
                continue
            callbacks=[button.get('callback_data','') for row in payload.get('reply_markup',{}).get('inline_keyboard',[]) for button in row]
            # Картки й робочі повідомлення звернення мають власний життєвий цикл.
            if not any(value.startswith(('n:','faq:','faqpage:')) for value in callbacks):
                continue
            with self.store.db:
                self.store.db.execute('DELETE FROM outbox WHERE is_menu=1 AND destination=?',(uid,))
                if self.store.draft(uid) and any('form_' in value for value in callbacks):
                    self.prompt_draft(uid)
                else:
                    state=self.nav_state(uid)
                    self.document_render(uid,self.document_route(uid,state['screen']),
                                         fresh_menu=payload.get('_fresh_menu',False),remember=False)

    def document_callback(self, uid, route):
        state = self.nav_state(uid)
        if route.startswith('doc_sources:'):
            origin = route[len('doc_sources:'):]
            if state['screen'] == origin and self.sources_for_answer(uid, origin):
                return self.document_sources(uid, origin)
            return
        if route == 'doc_sources_back':
            if state['screen'].startswith('sources:'):
                origin = state['screen'][len('sources:'):]
                return self.document_render(uid, self.document_route(uid, origin), remember=False)
            return
        if route == 'doc_back':
            target = state['history'].pop() if state['history'] else ('M02' if state['confirmed'] else 'M01')
            self.store.save_navigation(uid, state)
            return self.document_render(uid, target, remember=False)
        if route == 'doc_direction':
            state.update(direction=None, ratio=None, confirmed=False, help_answers={}, history=[])
            target = 'M01'
        elif route == 'doc_ratio':
            state.update(ratio=None, confirmed=False)
            target = 'R00'
        elif route == 'doc_legacy':
            return self.show_faq(uid)
        elif route.startswith('doc_page:'):
            value = route.partition(':')[2]
            state['faq_page'] = int(value) if value.isdecimal() and len(value)<6 else 0
            target = 'Q00'
        elif route.startswith('doc_input:'):
            if route != 'doc_input:H01' or state['screen'] != 's:H01':
                return
            state['helper_input'] = True
            self.store.save_navigation(uid, state)
            return self.document_render_helper_prompt(uid)
        elif route.startswith('doc_help:'):
            parts = route.split(':')
            if len(parts)!=3 or state['screen']!='s:'+parts[1] or parts[1] not in ('H01','H02','H03') or not parts[2].isdecimal():
                return
            buttons = self.document_content.screens[parts[1]]['buttons']
            index = int(parts[2])
            if index>=len(buttons) or buttons[index]['target'] not in ('H02','H03','H04'):
                return
            button = buttons[index]
            state['help_answers'][parts[1]] = button['label']
            for later in ('H01','H02','H03'):
                if later>parts[1]:
                    state['help_answers'].pop(later,None)
            target = button['target']
        elif route.startswith('s:'):
            target = route[2:]
            if target.startswith(('legacy_faq:', 'legacy_answer:')):
                return self.document_render(uid,target)
            if target not in self.document_content.screens or target.startswith(('P','E')):
                return
            if target in DIRECTIONS:
                state.update(direction=DIRECTIONS[target],ratio=None,confirmed=False,help_answers={},history=[])
            elif target in RATIOS:
                state.update(ratio=RATIOS[target],confirmed=False)
            elif target == 'M02' and self.document_route(uid,state['screen']) in RATIOS:
                state['confirmed'] = bool(state['direction'] and state['ratio'])
        else:
            return
        self.pending.pop(uid, None)
        self.faq_edits.pop(uid, None)
        self.store.save_navigation(uid, state)
        self.document_render(uid, target)

    def document_render_helper_prompt(self, uid):
        sid = self.document_route(uid,self.nav_state(uid)['screen'])
        sid = sid if sid in ('H01','H02','H03') else 'H01'
        self.nav_say(uid, self.document_content.text(sid) + '\n\n' + self.document_content.data['ui']['input'],
                     self.document_links(uid))

    def document_message(self, uid, text):
        state = self.nav_state(uid)
        sid = state['screen'][2:] if state['screen'].startswith('s:') else ''
        if not state['helper_input'] or sid not in ('H01','H02','H03'):
            return False
        if not text.strip() or size(text) > 500:
            self.document_render_helper_prompt(uid)
            return True
        state['help_answers'][sid] = text.strip()
        for later in ('H01','H02','H03'):
            if later>sid:
                state['help_answers'].pop(later,None)
        self.store.save_navigation(uid, state)
        self.document_render(uid, {'H01':'H02','H02':'H03','H03':'H04'}[sid])
        return True
