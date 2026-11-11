from __future__ import annotations

import time
import uuid

from .faq import FAQConflict, MAX_ITEMS, validate_answer, validate_question, validate_source

PAGE_SIZE = 6


def buttons(rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row] for row in rows]}


def page_number(value, count):
    number = int(value) if value.isdecimal() and len(value) < 7 else 0
    return min(max(number, 0), max(0, (count - 1) // PAGE_SIZE))


class FAQDialogs:
    """Customer navigation and role-checked dispatcher FAQ editing."""

    def show_faq(self, user_id, value='0', manage=False, *, notice=''):
        if manage and user_id != self.settings.operator_id:
            self.say(user_id, 'Редагування FAQ доступне лише диспетчеру.', self.menu(user_id))
            return
        items, revision = self.faq_catalog.snapshot()
        page = page_number(value, len(items))
        prefix = 'faqedit:' if manage else 'faq:'
        rows = [[(item['question'], prefix + item['id'])] for item in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        nav = []
        route = 'faqmanage' if manage else 'faqpage'
        if page:
            nav.append(('Назад', f'{route}:{page - 1}'))
        if (page + 1) * PAGE_SIZE < len(items):
            nav.append(('Далі', f'{route}:{page + 1}'))
        if nav:
            rows.append(nav)
        if user_id == self.settings.operator_id:
            rows.append([('Додати запитання', 'faqadd') if manage else ('Редагувати FAQ', 'faqmanage')])
        if manage:
            rows.append([('Панель диспетчера', 'admin')])
        else:
            rows.append([('Запитання диспетчеру', 'new')])
        rows.append([('Головне меню', 'menu')])
        title = 'Редагування FAQ' if manage else 'Поширені запитання про гранти на обладнання'
        self.say(user_id, f'{notice}{title}\nСторінка {page + 1} із {(len(items) - 1) // PAGE_SIZE + 1}. Оберіть запитання.', buttons(rows))

    def show_faq_answer(self, user_id, identifier, edit=False, *, notice=''):
        item = self.faq_catalog.get(identifier)
        if not item:
            self.say(user_id, 'Це запитання вже видалено. Оновіть список.', buttons([[('Оновити FAQ', 'faq')]]))
            return
        items, revision = self.faq_catalog.snapshot()
        index = next((i for i, entry in enumerate(items) if entry['id'] == identifier), 0)
        text = notice + item['question'] + '\n\n' + item['answer']
        if item.get('verified_at'):
            text += '\n\nІнформацію звірено: ' + item['verified_at'] + '.'
        elif item.get('updated_at'):
            text += '\n\nОновлено диспетчером: ' + item['updated_at'] + '.'
        rows = []
        if edit:
            rows.extend([[('Змінити запитання', f'faqfield:{identifier}:question')],
                         [('Змінити відповідь', f'faqfield:{identifier}:answer')],
                         [('Змінити джерело', f'faqfield:{identifier}:source_url')],
                         [('Видалити запитання', f'faqremove:{identifier}')]])
        rows.extend([[('До списку запитань', f'{"faqmanage" if edit else "faqpage"}:{index // PAGE_SIZE}')]])
        if not edit:
            rows.append([('Запитання диспетчеру', 'new')])
        rows.append([('Головне меню', 'menu')])
        markup = buttons(rows)
        if item.get('source_url'):
            markup['inline_keyboard'].insert(0, [{'text': 'Джерело інформації', 'url': item['source_url']}])
        self.say(user_id, text, markup)

    def faq_admin_callback(self, user_id, data):
        if user_id != self.settings.operator_id:
            self.faq_edits.pop(user_id, None)
            self.say(user_id, 'Редагування FAQ доступне лише диспетчеру.', self.menu(user_id))
            return
        parts = data.split(':')
        action = parts[0]
        identifier = parts[1] if len(parts) > 1 else ''
        self.pending.pop(user_id, None)
        self.faq_edits.pop(user_id, None)
        if action == 'faqmanage':
            self.show_faq(user_id, identifier, manage=True)
        elif action == 'faqedit':
            self.show_faq_answer(user_id, identifier, edit=True)
        elif action == 'faqadd':
            items, revision = self.faq_catalog.snapshot()
            if len(items) >= MAX_ITEMS:
                self.say(user_id, f'У каталозі вже {MAX_ITEMS} запитань. Спочатку видаліть зайве.')
                return
            self.pending.pop(user_id, None)
            self.faq_edits[user_id] = {'action': 'add', 'field': 'question', 'revision': revision,
                                       'item': {'id': uuid.uuid4().hex[:20]}, 'expires': time.monotonic() + 1800}
            self.say(user_id, 'Введіть нове запитання одним рядком, до 60 одиниць UTF-16. Скасувати: /cancel.')
        elif action == 'faqfield' and len(parts) == 3 and parts[2] in ('question', 'answer', 'source_url'):
            if not self.faq_catalog.get(identifier):
                self.show_faq(user_id, manage=True)
                return
            self.pending.pop(user_id, None)
            field = parts[2]
            self.faq_edits[user_id] = {'action': 'update', 'id': identifier, 'field': field,
                                       'revision': self.faq_catalog.revision, 'expires': time.monotonic() + 1800}
            prompts = {'question': 'Введіть нове запитання одним рядком, до 60 одиниць UTF-16.',
                       'answer': 'Введіть нову відповідь, до 3000 одиниць UTF-16. Після зміни попередня позначка перевірки та джерело прибираються; джерело можна додати окремо.',
                       'source_url': 'Надішліть HTTPS-посилання на джерело або —, щоб прибрати посилання.'}
            self.say(user_id, prompts[field] + ' Скасувати: /cancel.')
        elif action == 'faqremove':
            if not self.faq_catalog.get(identifier):
                self.show_faq(user_id, manage=True)
                return
            self.say(user_id, 'Видалити це запитання з FAQ? У каталозі має залишитися хоча б одне запитання.',
                     buttons([[('Так, видалити', f'faqdelete:{identifier}:{self.faq_catalog.revision}')],
                              [('Скасувати', 'faqmanage')]]))
        elif action == 'faqdelete' and len(parts) == 3 and parts[2].isdecimal() and len(parts[2]) < 16:
            try:
                self.faq_catalog.delete(identifier, int(parts[2]))
            except (ValueError, OSError, RuntimeError) as error:
                self.faq_error(user_id, error)
                return
            self.show_faq(user_id, manage=True, notice='Запитання видалено. Зміни вже доступні клієнтам.\n\n')
        else:
            self.say(user_id, 'Оновіть список FAQ.', buttons([[('Редагувати FAQ', 'faqmanage')]]))

    def faq_error(self, user_id, error):
        if isinstance(error, ValueError):
            text = str(error)
        else:
            text = 'Не вдалося зберегти FAQ. Перевірте доступ до папки data та оновіть список.'
        self.say(user_id, text, buttons([[('Оновити FAQ', 'faqmanage')], [('Головне меню', 'menu')]]))

    def faq_admin_message(self, user_id, text):
        state = self.faq_edits.get(user_id)
        if not state:
            return False
        if user_id != self.settings.operator_id:
            self.faq_edits.pop(user_id, None)
            return False
        if state['expires'] < time.monotonic():
            self.faq_edits.pop(user_id, None)
            self.say(user_id, 'Час редагування минув. Відкрийте FAQ і повторіть дію.', buttons([[('Редагувати FAQ', 'faqmanage')]]))
            return True
        field = state['field']
        try:
            value = {'question': validate_question, 'answer': validate_answer, 'source_url': validate_source}[field](text)
        except ValueError as error:
            self.say(user_id, str(error) + ' Спробуйте ще раз або /cancel.')
            return True
        if state['action'] == 'add':
            state['item'][field] = value
            if field in ('question', 'answer'):
                state['field'] = 'answer' if field == 'question' else 'source_url'
                prompt = 'Введіть відповідь, до 3000 одиниць UTF-16.' if field == 'question' else 'Надішліть HTTPS-посилання на джерело або —, якщо джерела немає.'
                self.say(user_id, prompt + ' Скасувати: /cancel.')
                return True
        try:
            if state['action'] == 'add':
                self.faq_catalog.add(state['item'], state['revision'])
            else:
                self.faq_catalog.update(state['id'], {field: value}, state['revision'])
        except (FAQConflict, OSError, RuntimeError) as error:
            self.faq_edits.pop(user_id, None)
            self.faq_error(user_id, error)
            return True
        except ValueError as error:
            self.faq_error(user_id, error)
            return True
        self.faq_edits.pop(user_id, None)
        self.show_faq_answer(user_id, state['item']['id'] if state['action'] == 'add' else state['id'],
                             edit=True, notice='FAQ збережено. Зміни вже доступні клієнтам.\n\n')
        return True
