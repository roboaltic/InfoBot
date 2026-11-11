"""Versioned, durable client routes; legal content is data, never eligibility logic."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .settings import ROOT


class NavigationContent:
    def __init__(self, path: Path | None = None):
        self.data = json.loads((path or ROOT / 'content' / 'navigation.json').read_text(encoding='utf-8'))
        data = self.data
        if data.get('version') != 1 or not all(isinstance(data.get(key), dict) for key in
                ('directions', 'ratios', 'labels', 'sections', 'answers', 'combinations', 'faq_bindings', 'texts', 'support')):
            raise ValueError('Некоректний каталог навігації.')
        for collection in ('directions', 'ratios', 'sections', 'answers'):
            if not data[collection] or any(not re.fullmatch(r'[a-z0-9_]{1,24}', key) for key in data[collection]):
                raise ValueError('Некоректні ідентифікатори каталогу.')
        for labels in (data['directions'], data['ratios'], data['labels']):
            for label in labels.values():
                if not isinstance(label, str) or not label.strip() or len(label.encode('utf-16-le')) // 2 > 60:
                    raise ValueError('Підпис кнопки має містити від 1 до 60 одиниць UTF-16.')
        required_labels = ('support', 'back', 'grant', 'direction', 'ratio', 'confirm', 'choose_help',
                           'ratio_help', 'faq', 'previous_page', 'next_page', 'source', 'admin', 'send',
                           'edit', 'cancel', 'unregistered', 'choose_direction', 'mine', 'resume')
        if any(key not in data['labels'] for key in required_labels):
            raise ValueError('Відсутні підписи кнопок.')
        questions = data.get('selection_questions')
        if not isinstance(questions, list) or len(questions) > 8:
            raise ValueError('Некоректні запитання підбору.')
        identifiers = set()
        for question in questions:
            if (not isinstance(question, dict) or not re.fullmatch(r'[a-z0-9_]{1,24}', question.get('id', ''))
                    or question['id'] in identifiers or question.get('status') not in ('pending', 'draft', 'approved')
                    or not isinstance(question.get('text'), str) or not 1 <= len(question['text']) <= 250):
                raise ValueError('Некоректне запитання підбору.')
            identifiers.add(question['id'])
        children = [child for section in data['sections'].values() for child in section['children']]
        if len(children) != len(set(children)) or set(children) != set(data['answers']):
            raise ValueError('Кожен підрозділ має належати рівно одному розділу.')
        for section in data['sections'].values():
            self.validate_title(section.get('title'))
            if not section['children'] or any(key not in data['answers'] for key in section['children']):
                raise ValueError('Невідомий підрозділ каталогу.')
        for answer in data['answers'].values():
            self.validate_title(answer.get('title'))
            self.validate_record(answer['common'])
            for key, record in answer['overrides'].items():
                direction, _, ratio = key.partition(':')
                if direction not in data['directions'] or (ratio and ratio not in data['ratios']):
                    raise ValueError('Невідомий сценарій нормативного тексту.')
                self.validate_record(record)
        for direction, combinations in data['combinations'].items():
            if direction not in data['directions']:
                raise ValueError('Невідомий напрям у матриці.')
            for ratio, record in combinations.items():
                if ratio not in data['ratios']:
                    raise ValueError('Невідоме співфінансування у матриці.')
                self.validate_record(record)
                if record.get('availability') not in ('unconfirmed', 'available', 'unavailable'):
                    raise ValueError('Некоректна матриця поєднань.')
        bindings = data['faq_bindings']
        if not isinstance(bindings.get('common'), list) or not isinstance(bindings.get('directions'), dict) or not isinstance(bindings.get('combinations'), dict):
            raise ValueError('Некоректна прив’язка FAQ.')
        for key in bindings['directions']:
            if key not in data['directions']:
                raise ValueError('Невідомий напрям FAQ.')
        for key in bindings['combinations']:
            direction, _, ratio = key.partition(':')
            if direction not in data['directions'] or ratio not in data['ratios']:
                raise ValueError('Невідомий сценарій FAQ.')
        for values in [bindings['common'], *bindings['directions'].values(), *bindings['combinations'].values()]:
            if not isinstance(values, list) or any(not isinstance(key, str) or (key != '*' and not re.fullmatch(r'[a-z0-9_-]{1,24}', key)) for key in values):
                raise ValueError('Некоректні ідентифікатори FAQ.')

    @staticmethod
    def validate_title(title):
        if (not isinstance(title, str) or not title.strip() or '\n' in title or '\r' in title
                or len(title.encode('utf-16-le')) // 2 > 60):
            raise ValueError('Назва розділу має містити від 1 до 60 одиниць UTF-16 одним рядком.')

    @staticmethod
    def validate_record(record):
        if record.get('status') not in ('pending', 'draft', 'approved'):
            raise ValueError('Некоректний статус тексту.')
        for key in ('summary', 'conditions', 'next_step', 'source_url', 'source_point'):
            if not isinstance(record.get(key), str) or len(record[key]) > 700:
                raise ValueError('Некоректне поле нормативного тексту.')
        url = record['source_url']
        parsed = urlsplit(url)
        if url and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or any(c.isspace() for c in url)):
            raise ValueError('Джерело має бути HTTPS-посиланням.')
        if record['status'] == 'approved' and (not url or any(not record[key].strip() for key in ('summary', 'conditions', 'next_step', 'source_point'))):
            raise ValueError('Для затвердженого тексту потрібні джерело та пункт.')

    def record_text(self, record):
        if record.get('status') != 'approved':
            # Unreviewed legal drafts are never presented as current rules.
            return self.data['texts']['pending']
        return self.data['texts']['approved'].format(**record)


class ClientNavigation:
    def nav_state(self, uid):
        raw = self.store.navigation(uid)
        direction = raw.get('direction') if isinstance(raw.get('direction'), str) and raw.get('direction') in self.content.data['directions'] else None
        ratio = raw.get('ratio') if direction and isinstance(raw.get('ratio'), str) and raw.get('ratio') in self.content.data['ratios'] else None
        revision = raw.get('revision', 0)
        return {'version': 1, 'direction': direction, 'ratio': ratio,
                'confirmed': bool(direction and ratio and raw.get('confirmed')),
                'screen': raw.get('screen', 'home') if isinstance(raw.get('screen', 'home'), str) else 'home',
                'revision': revision if type(revision) is int and revision >= 0 else 0}

    def nav_markup(self, uid, rows):
        state = self.nav_state(uid)
        state['revision'] += 1
        self.store.save_navigation(uid, state)
        return {'inline_keyboard': [[{'text': label, 'callback_data': f'n:{state["revision"]}:{route}'}
                                    for label, route in row] for row in rows]}

    def nav_say(self, uid, text, rows, *, source_url='', **kwargs):
        markup = self.nav_markup(uid, rows)
        if source_url:
            markup['inline_keyboard'].insert(0, [{'text': self.nav_label('source'), 'url': source_url}])
        self.say(uid, text, markup, **kwargs)

    def nav_label(self, name):
        return self.content.data['labels'][name]

    def nav_links(self, uid, back):
        rows = [[(self.nav_label('back'), back)]]
        state = self.nav_state(uid)
        if state['confirmed']:
            rows.append([(self.nav_label('grant'), 'grant')])
        rows.append([(self.nav_label('direction'), 'change_direction')])
        if state['direction']:
            rows.append([(self.nav_label('ratio'), 'change_ratio')])
        rows.append([(self.nav_label('support'), 'support')])
        return rows

    def grant_title(self, uid):
        state = self.nav_state(uid)
        d = self.content.data
        return d['directions'].get(state['direction'], d['texts']['no_direction']) + ' · ' + (
            state['ratio'].replace('_', '/') if state['ratio'] else d['texts']['no_ratio'])

    def client_home(self, uid):
        self.render_nav(uid, 'home')

    def client_return(self, uid):
        self.render_nav(uid, 'grant' if self.nav_state(uid)['confirmed'] else 'home')

    def render_nav(self, uid, route=None):
        state = self.nav_state(uid)
        route = route or state['screen']
        data = self.content.data
        action, _, value = route.partition(':')
        if action == 'legacy_faq':
            self.show_faq(uid, value)
            return
        if action == 'legacy_answer':
            self.show_faq_answer(uid, value)
            return
        if action not in ('home', 'choose_help') and not state['direction']:
            route, action, value = 'home', 'home', ''
        if action in ('grant', 'section', 'answer', 'faq', 'faqanswer') and not state['confirmed']:
            route, action, value = 'ratios', 'ratios', ''
        state['screen'] = route
        self.store.save_navigation(uid, state)
        rows = []
        source_url = ''
        if action in ('home', 'choose_help'):
            text = data['greeting'] if action == 'home' else data['direction_help']
            rows = [[(label, 'direction:' + key)] for key, label in data['directions'].items()]
            if action == 'home':
                rows.append([(self.nav_label('choose_help'), 'choose_help')])
            else:
                rows.append([(self.nav_label('back'), 'home')])
            rows.append([(self.nav_label('support'), 'support')])
            if uid == self.settings.operator_id:
                rows.append([(self.nav_label('admin'), 'admin')])
        elif action in ('ratios', 'ratio_help'):
            text = self.grant_title(uid) + '\n\n' + (data['ratio_help'] if action == 'ratio_help' else data['texts']['ratio_prompt'])
            if action == 'ratio_help':
                for question in data['selection_questions']:
                    if question['status'] == 'approved':
                        text += '\n\n' + question['text']
            rows = [[(label, 'ratio:' + key)] for key,label in data['ratios'].items()]
            if action == 'ratios':
                rows.append([(self.nav_label('ratio_help'), 'ratio_help')])
            rows += self.nav_links(uid, 'ratios') if action == 'ratio_help' else [[(self.nav_label('back'), 'home')]]
        elif action == 'preview':
            if not state['ratio']:
                return self.render_nav(uid, 'ratios')
            record = data['combinations'].get(state['direction'], {}).get(state['ratio'], {})
            text = self.grant_title(uid) + '\n\n' + self.content.record_text(record) + '\n\n' + data['selection_notice']
            rows = [[(self.nav_label('ratio'), 'change_ratio')],[(self.nav_label('support'), 'support')]]
            if record.get('status') == 'approved' and record.get('availability') == 'unavailable':
                text += '\n\n' + data['texts']['unavailable']
            else:
                rows.insert(0, [(self.nav_label('confirm'), 'confirm')])
        elif action == 'grant':
            text = self.grant_title(uid) + '\n\n' + data['texts']['section_prompt']
            rows = [[(section['title'], 'section:'+key)] for key,section in data['sections'].items()]
            rows += [[(self.nav_label('faq'),'faq:0')],[(self.nav_label('support'),'support')],[(self.nav_label('direction'),'change_direction')],[(self.nav_label('ratio'),'change_ratio')]]
        elif action == 'section' and value in data['sections']:
            section = data['sections'][value]
            text = self.grant_title(uid) + '\n\n' + section['title']
            rows = [[(data['answers'][key]['title'],'answer:'+key)] for key in section['children']]
            rows += self.nav_links(uid, 'grant')
        elif action == 'answer' and value in data['answers']:
            answer = data['answers'][value]
            overrides = answer['overrides']
            record = overrides.get(state['direction']+':'+state['ratio'], overrides.get(state['direction'], answer['common']))
            parent = next(key for key,section in data['sections'].items() if value in section['children'])
            text = self.grant_title(uid) + '\n\n' + answer['title'] + '\n\n' + self.content.record_text(record)
            rows = self.nav_links(uid,'section:'+parent)
        elif action in ('faq', 'faqanswer'):
            items = self.client_faq_items(uid)
            if action == 'faq':
                page = min(int(value) if value.isdecimal() and len(value)<7 else 0, max(0,(len(items)-1)//6))
                text = self.grant_title(uid) + '\n\n' + data['texts']['faq_page'].format(page=page+1)
                rows = [[(item['question'],'faqanswer:'+item['id'])] for item in items[page*6:(page+1)*6]]
                if page: rows.append([(self.nav_label('previous_page'),f'faq:{page-1}')])
                if (page+1)*6<len(items): rows.append([(self.nav_label('next_page'),f'faq:{page+1}')])
                rows += self.nav_links(uid,'grant')
            else:
                item = next((item for item in items if item['id']==value),None)
                if not item:
                    return self.render_nav(uid,'faq:0')
                page = items.index(item)//6
                text = self.grant_title(uid)+'\n\n'+item['question']+'\n\n'+item['answer']+'\n\n'+data['texts']['faq_general']
                if item.get('verified_at'): text+='\n\n'+data['texts']['faq_verified'].format(date=item['verified_at'])
                elif item.get('updated_at'): text+='\n\n'+data['texts']['faq_updated'].format(date=item['updated_at'])
                source_url = item.get('source_url', '')
                rows = self.nav_links(uid,f'faq:{page}')
        else:
            return self.render_nav(uid,'home')
        self.nav_say(uid,text,rows,source_url=source_url)

    def open_client_faq(self, uid):
        if self.nav_state(uid)['confirmed']:
            self.render_nav(uid, 'faq:0')
        else:
            self.show_faq(uid)

    def client_faq_items(self, uid):
        state = self.nav_state(uid)
        bindings = self.content.data['faq_bindings']
        identifiers = bindings['common'] + bindings['directions'].get(state['direction'],[]) + bindings['combinations'].get(str(state['direction'])+':'+str(state['ratio']),[])
        return [item for item in self.faq if '*' in identifiers or item['id'] in identifiers]

    def client_nav_callback(self, uid, data, message_id=None):
        parts=data.split(':',2)
        state=self.nav_state(uid)
        if len(parts)!=3 or parts[1]!=str(state['revision']):
            return
        route=parts[2]
        action,_,value=route.partition(':')
        self.pending.pop(uid,None)
        self.faq_edits.pop(uid,None)
        if action=='admin':
            self.admin(uid)
            return
        if action=='mine':
            self.callback(uid, 'mine')
            return
        if action=='support':
            self.begin_ticket(uid)
            return
        if action.startswith('form_'):
            self.form_callback(uid,action)
            return
        if action=='change_direction':
            state.update(direction=None,ratio=None,confirmed=False)
            route='home'
        elif action=='direction' and value in self.content.data['directions']:
            state.update(direction=value,ratio=None,confirmed=False)
            route='ratios'
        elif action=='change_ratio' and state['direction']:
            state.update(ratio=None,confirmed=False)
            route='ratios'
        elif action=='ratio' and state['direction'] and value in self.content.data['ratios']:
            state.update(ratio=value,confirmed=False)
            route='preview'
        elif action=='confirm':
            if not state['direction'] or not state['ratio']: return self.render_nav(uid,'home')
            record=self.content.data['combinations'].get(state['direction'],{}).get(state['ratio'],{})
            if record.get('status')=='approved' and record.get('availability')=='unavailable':
                return self.render_nav(uid,'preview')
            state['confirmed']=True
            route='grant'
        self.store.save_navigation(uid,state)
        self.render_nav(uid,route)
