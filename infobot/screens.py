"""Каталог документа 0.1: дані, перевірка та навігація без оцінки права на грант."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from string import Formatter

from .faq import size, validate_source, validate_items
from .settings import ROOT

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
        self.faq_defaults = validate_items(json.loads(path.with_name('faq_v01.json').read_text(encoding='utf-8')))
        self.validate()

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

    def text(self, sid, **values):
        body = self.screens[sid].get('body', '')
        # Telegram plain text: user input never becomes HTML or Markdown.
        fields = [field for _, field, _, _ in Formatter().parse(body) if field]
        if any(field not in values for field in fields):
            raise ValueError('Не вистачає підстановок екрана ' + sid)
        return body.format_map(values)


class DocumentNavigation:
    def document_route(self, uid, route):
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
        return [item for item in self.faq if item.get('screen_id') and
                ('*' in item.get('directions', ['*']) or state['direction'] in item['directions']) and
                ('*' in item.get('ratios', ['*']) or state['ratio'] in item['ratios'])]

    def document_render(self, uid, sid, *, fresh_menu=False, remember=True):
        catalog = self.document_content
        state = self.nav_state(uid)
        if sid not in catalog.screens:
            sid = 'M02' if state['confirmed'] else 'M01'
        if sid in ('R00', 'R50', 'R70', 'R80', 'H01', 'H02', 'H03', 'H04') and not state['direction']:
            sid = 'M01'
        if sid == 'M02' and not state['confirmed']:
            sid = 'R00' if state['direction'] else 'M01'
        old = self.document_route(uid, state['screen'])
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
        if sid in RATIOS:
            text += '\n\n' + catalog.data['selection_notice']
        if sid.startswith(('F', 'A', 'B', 'Q', 'R')) and sid not in ('Q00', 'R00'):
            text += '\n\n' + catalog.data['legal_notice']
            text += '\n\n' + catalog.data['source_notice']
        if sid == 'H04':
            text += '\n\n' + ui['helper_summary'] + '\n' + '\n'.join(state['help_answers'].values())
            text += '\n\n' + ui['helper_notice']
        if screen['source_ids']:
            # Internal source IDs and editorial status never appear to the client.
            source_points = item.get('source_points','') if sid.startswith('Q') and sid!='Q00' else screen['source_points']
            points = re.search(r'пункт(?:и|у)? ([0-9, –—-]+)', source_points)
            if points:
                text += '\n\n' + ui['sources_points'] + ' ' + points[1].strip()
        if sid.startswith('Q') and sid!='Q00' and item.get('as_of'):
            text += '\n\nІнформація станом на ' + item['as_of']
        if sid not in ('M01', 'D04') and state['direction']:
            title = self.grant_title(uid) + '\n\n' + title
        markup = self.nav_markup(uid, rows)
        if sid.startswith('Q') and sid != 'Q00':
            source_urls = item.get('sources', [item['source_url']] if item.get('source_url') else [])
            for url in source_urls:
                source_id = next((key for key,source in catalog.data['sources'].items() if source['url']==url),None)
                label = next((button['label'] for button in screen['buttons'] if button['kind']=='source' and button['target']==source_id), ui['source'])
                markup['inline_keyboard'].insert(0, [{'text': label, 'url': url}])
        else:
            for source_id in reversed(screen['source_ids']):
                source = catalog.data['sources'][source_id]
                if source['url']:
                    label = next((button['label'] for button in screen['buttons'] if button['kind']=='source' and button['target']==source_id), source['title'])
                    markup['inline_keyboard'].insert(0, [{'text': label, 'url': source['url']}])
        body = title + '\n\n' + text
        if size(body) > 4096:
            raise ValueError('Текст екрана перевищує ліміт Telegram.')
        self.say(uid, body, markup, fresh_menu=fresh_menu)

    def document_callback(self, uid, route):
        state = self.nav_state(uid)
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
        self.nav_say(uid, self.document_content.text('H01') + '\n\n' + self.document_content.data['ui']['input'],
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
