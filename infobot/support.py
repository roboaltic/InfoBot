"""Persistent support intake with explicit review and submission."""
from __future__ import annotations

import json
import re
import time

from .contacts import complete, validate_step

FORM_STEPS = ('question', 'full_name', 'company', 'phone', 'email', 'review')
CONTACT_FIELDS = ('client_email', 'client_phone', 'client_full_name', 'client_first_name', 'client_company')
FORM_FIELDS = {'question': 'question', 'full_name': 'client_full_name', 'company': 'client_company',
               'phone': 'client_phone', 'email': 'client_email'}


def organization(text):
    value = ' '.join(text.strip().split())
    if not 2 <= len(value) <= 180 or any(char in text for char in '\r\n') or not any(c.isalnum() for c in value):
        raise ValueError('Укажіть компанію або ФОП одним рядком, від 2 до 180 знаків.')
    return value


class ClientSupport:
    def support(self, uid):
        self.begin_ticket(uid)

    def begin_ticket(self, uid):
        if uid == self.settings.operator_id:
            self.admin(uid)
            return
        if self.store.draft(uid):
            self.prompt_draft(uid)
            return
        ticket = self.store.user_ticket(uid)
        if ticket and complete(ticket):
            self.nav_say(uid, self.content.data['support']['existing'].format(number=ticket['id']), [
                [(self.nav_label('mine'), 'mine')],
                [(self.nav_label('grant'), 'grant')] if self.nav_state(uid)['confirmed'] else
                [(self.nav_label('choose_direction'), 'home')],
            ])
            return
        state = self.nav_state(uid)
        self.store.create_draft(uid, '', '', '', 0, time.time(), ticket['id'] if ticket else None)
        values = {'step': 'question', 'form_version': 2,
                  'grant_context': json.dumps({'origin': state['screen']}, ensure_ascii=False)}
        if ticket:
            values.update({field: ticket[field] for field in CONTACT_FIELDS})
        self.store.update_draft(uid, **values)
        self.prompt_draft(uid)

    def migrate_draft(self, uid):
        draft = self.store.draft(uid)
        if not draft:
            return None
        if draft['form_version'] != 2 or draft['step'] not in FORM_STEPS:
            step = next((key for key, field in FORM_FIELDS.items() if not draft[field]), 'review')
            self.store.update_draft(uid, step=step, form_version=2)
        return self.store.draft(uid)

    def context(self, uid, origin):
        state = self.nav_state(uid)
        return {'direction': state['direction'], 'ratio': state['ratio'], 'origin': origin}

    def context_summary(self, context):
        data = self.content.data
        messages = data['support']
        direction = data['directions'].get(context.get('direction'), messages['missing'])
        ratio = data['ratios'].get(context.get('ratio'), messages['missing'])
        action, _, value = context.get('origin', 'home').partition(':')
        if action == 'answer':
            origin = data['answers'].get(value, {}).get('title', messages['previous_section'])
        elif action == 'section':
            origin = data['sections'].get(value, {}).get('title', messages['previous_section'])
        elif action in ('faqanswer', 'legacy_answer'):
            item = self.faq_catalog.get(value)
            origin = item['question'] if item else messages['deleted_faq']
        else:
            origin = messages['origin_labels'].get(action, messages['unknown_origin'])
        return messages['context'].format(direction=direction, ratio=ratio, origin=origin)

    def prompt_draft(self, uid, prefix=''):
        draft = self.migrate_draft(uid)
        if not draft:
            return
        step = draft['step']
        messages = self.content.data['support']
        if step == 'review':
            context = json.loads(draft['grant_context'])
            text = messages['review'].format(
                question=draft['question'], contacts=self.contact_summary(draft),
                context=self.context_summary(self.context(uid, context.get('origin', 'home'))))
            rows = [[(self.nav_label('send'), 'form_send')], [(self.nav_label('edit'), 'form_edit')],
                    [(self.nav_label('back'), 'form_back')], [(self.nav_label('cancel'), 'form_cancel')]]
        else:
            text = messages['prompts'][step]
            if draft[FORM_FIELDS[step]]:
                text += '\n\n' + messages['saved'].format(value=draft[FORM_FIELDS[step]])
            rows = []
            if step == 'company':
                rows.append([(self.nav_label('unregistered'), 'form_unregistered')])
            rows += [[(self.nav_label('back'), 'form_back')], [(self.nav_label('cancel'), 'form_cancel')]]
        self.nav_say(uid, (prefix + '\n\n' if prefix else '') + text, rows, draft_user_id=uid)

    def back_draft(self, uid):
        draft = self.migrate_draft(uid)
        if not draft:
            self.client_return(uid)
            return
        index = FORM_STEPS.index(draft['step'])
        if index == 0:
            # Leave the form without destroying the saved question or contacts.
            self.render_nav(uid)
            return
        self.store.update_draft(uid, step=FORM_STEPS[index - 1])
        self.prompt_draft(uid)

    def cancel_draft(self, uid):
        self.store.cancel_draft(uid, self.settings.delete_telegram_on_close)
        self.pending.pop(uid, None)
        self.render_nav(uid)

    def form_callback(self, uid, action):
        draft = self.migrate_draft(uid)
        if not draft:
            self.client_return(uid)
            return
        if action == 'form_send' and draft['step'] == 'review':
            self.submit_draft(uid)
        elif action == 'form_edit':
            self.store.update_draft(uid, step='question')
            self.prompt_draft(uid)
        elif action == 'form_back':
            self.back_draft(uid)
        elif action == 'form_cancel':
            self.cancel_draft(uid)
        elif action == 'form_unregistered' and draft['step'] == 'company':
            self.store.update_draft(uid, client_company=self.nav_label('unregistered'), step='phone')
            self.prompt_draft(uid)
        else:
            self.prompt_draft(uid)

    def advance_draft(self, uid, text, message):
        draft = self.migrate_draft(uid)
        step = draft['step']
        if step == 'review':
            self.prompt_draft(uid, self.content.data['support']['send_hint'])
            return
        self.store.track_draft(uid, message['message_id'], message.get('date', time.time()))
        try:
            if step == 'question':
                if not text.strip() or len(text.encode('utf-16-le')) // 2 > 2000:
                    raise ValueError('Запитання має містити від 1 до 2000 одиниць UTF-16.')
                values = {'question': text}
            elif step == 'company':
                values = {'client_company': organization(text)}
            else:
                values = validate_step(step, text)
        except ValueError as error:
            self.prompt_draft(uid, str(error))
            return
        actor = message.get('from', {})
        name = ' '.join(str(actor.get(key, '')).replace('\n', ' ').replace('\r', ' ')
                        for key in ('first_name', 'last_name')).strip()[:200]
        username = str(actor.get('username', ''))
        username = username if re.fullmatch(r'[A-Za-z0-9_]{1,32}', username) else ''
        self.store.db.execute('UPDATE drafts SET telegram_name=?,telegram_username=? WHERE user_id=?',
                              (name, username, uid))
        self.store.update_draft(uid, **values, step=FORM_STEPS[FORM_STEPS.index(step) + 1])
        self.prompt_draft(uid)

    def submit_draft(self, uid):
        draft = self.migrate_draft(uid)
        if not draft or draft['step'] != 'review':
            return
        if not complete(draft) or not draft['question'].strip():
            self.store.update_draft(uid, step='question')
            self.prompt_draft(uid, self.content.data['support']['incomplete'])
            return
        if not self.settings.operator_id and not self.settings.mail.enabled:
            self.prompt_draft(uid, self.content.data['support']['unconnected'])
            return
        ticket_id = draft['ticket_id'] or self.store.create_ticket(
            uid, draft['telegram_name'], draft['telegram_username'])
        self.store.set_contacts(ticket_id, draft)
        context = self.context(uid, json.loads(draft['grant_context']).get('origin', 'home'))
        self.store.db.execute('UPDATE tickets SET grant_context=? WHERE id=?',
                              (json.dumps(context, ensure_ascii=False), ticket_id))
        refs = [item for item in json.loads(draft['message_refs']) if item['id'] > 0]
        question = draft['question']
        self.store.delete_draft(uid)
        self.accept_question(uid, ticket_id, question, refs, show_contacts=True)
