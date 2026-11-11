from __future__ import annotations

import copy
import json
import re
import secrets
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable

from .mailer import MailTemplate
from .settings import Settings
from .store import Store
from .contacts import STEPS, PROMPTS, address_name, complete as contacts_complete, validate_step
from .faq import FAQCatalog, load_faq
from .faq_dialog import FAQDialogs


def text_size(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def shorten(text: str, units: int = 850) -> str:
    if text_size(text) <= units:
        return text
    return text.encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore') + '…'


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


class Pairing:
    def __init__(self):
        self.code = ''
        self.expires = 0.0
        self.attempts = 0

    def issue(self) -> str:
        self.code = secrets.token_urlsafe(18)
        self.expires = time.monotonic() + 600
        self.attempts = 0
        return self.code

    def redeem(self, candidate: str) -> bool:
        if not self.code or time.monotonic() >= self.expires or self.attempts >= 20:
            return False
        self.attempts += 1
        if not secrets.compare_digest(candidate.encode('utf-8'), self.code.encode('utf-8')):
            return False
        self.code = ''
        return True


class Engine(FAQDialogs):
    """Business logic writes to durable queues; it never sends HTTP or SMTP."""
    def __init__(self, store: Store, settings: Settings, faq: list[dict],
                 on_operator: Callable[[int], None] | None = None,
                 mail_template: MailTemplate | None = None, bot_username: str = '',
                 faq_catalog: FAQCatalog | None = None):
        self.store, self.settings = store, settings
        self.faq_catalog = faq_catalog or FAQCatalog(faq)
        self.on_operator = on_operator or (lambda user_id: None)
        self.mail_template = mail_template or MailTemplate()
        self.bot_username = '@' + bot_username if bot_username else 'InfoBot'
        self.pairing = Pairing()
        self.pending: OrderedDict[int, tuple[str, int, float]] = OrderedDict()
        self.faq_edits: dict[int, dict] = {}

    @property
    def faq(self):
        return self.faq_catalog.snapshot()[0]

    def _pending(self, user_id: int, action: str, ticket_id: int = 0):
        self.pending[user_id] = (action, ticket_id, time.monotonic() + 1800)
        self.pending.move_to_end(user_id)
        while len(self.pending) > 2000:
            self.pending.popitem(last=False)

    def menu(self, user_id: int):
        rows = [[('Поширені запитання', 'faq')], [('Підтримка', 'support')],
                [('Запитання диспетчеру', 'new')]]
        if user_id == self.settings.operator_id:
            rows.append([('Панель диспетчера', 'admin')])
        return keyboard(rows)

    def support(self, user_id: int):
        rows = [[('Моє звернення', 'mine')]]
        draft = self.store.draft(user_id)
        if draft:
            rows.insert(0, [('Продовжити анкету', 'continue')])
            if draft['step'] != 'email':
                rows.append([('Попередній крок', 'back')])
            rows.append([('Скасувати чернетку', 'cancel_draft')])
        rows.extend([[('Про мої дані', 'privacy'), ('Мій Telegram ID', 'identity')],
                     [('Головне меню', 'menu')]])
        self.say(user_id, 'Підтримка\n\nОберіть потрібну дію.', keyboard(rows))

    def admin(self, user_id: int, notice: str = ''):
        if user_id != self.settings.operator_id:
            self.say(user_id, 'Панель доступна лише диспетчеру.', self.menu(user_id))
            return
        self.say(user_id, notice + 'Панель диспетчера\n\nОберіть звернення для відповіді або змініть каталог FAQ. '
                 'Налаштування пошти, доступу та резервні копії — у програмі на комп’ютері.',
                 keyboard([[('Відкриті звернення', 'tickets:0')], [('Редагувати FAQ', 'faqmanage')],
                           [('Команди диспетчера', 'commands')], [('Головне меню', 'menu')]]))

    def say(self, user_id: int, text: str, markup: dict | None = None, ticket_id: int | None = None,
            draft_user_id: int | None = None, *, notification: bool = False):
        payload = {'chat_id': user_id, 'text': text}
        if markup is None and not notification:
            markup = keyboard([[('Головне меню', 'menu')]])
        if markup is not None:
            payload['reply_markup'] = markup
        self.store.enqueue('sendMessage', payload, ticket_id, draft_user_id, is_menu=not notification)

    def _can_access(self, user_id: int, ticket) -> bool:
        return bool(ticket and user_id in (ticket['user_id'], self.settings.operator_id))

    def process(self, update: dict):
        update_id = update.get('update_id')
        if type(update_id) is not int or update_id < self.store.offset:
            return
        with self._transaction():
            callback = update.get('callback_query')
            message = callback.get('message', {}) if callback else update.get('message', {})
            actor = callback.get('from', {}) if callback else message.get('from', {})
            user_id = actor.get('id')
            chat = message.get('chat', {})
            if type(user_id) is int and user_id > 0 and not actor.get('is_bot') and chat.get('type') == 'private' and chat.get('id') == user_id:
                text = message.get('text', '')
                command = text.split(maxsplit=1)[0].split('@')[0].lower() if text.startswith('/') else ''
                if command == '/claim' and not callback:
                    self.claim(user_id, text)
                    if self.settings.operator_id == user_id:
                        self.store.record_connection(user_id, actor)
                elif (not self.settings.allowed_user_ids or user_id in self.settings.allowed_user_ids
                      or user_id == self.settings.operator_id):
                    self.store.record_connection(user_id, actor)
                    if callback:
                        self.store.enqueue('answerCallbackQuery', {'callback_query_id': callback['id']})
                        data = callback.get('data', '')
                        # Older closure notifications used the static menu callback.
                        if (data == 'menu'
                                and message.get('reply_markup') == keyboard([[('Головне меню', 'menu')]])
                                and re.fullmatch(
                                    r'(?:.*дякуємо за звернення! Ваше звернення|Звернення) №[0-9]+ закрите\. '
                                    r'Листування видалено з робочої бази\.'
                                    r'(?: Уже надіслані електронні листи залишаються у служби підтримки\.)?',
                                    message.get('text', ''))):
                            data = 'notification_menu'
                        self.callback(user_id, data, message.get('message_id'))
                    else:
                        self.message(user_id, message, command)
                else:
                    self.say(user_id, 'Цей тестовий бот доступний лише учасникам тестування.')
                message_id = message.get('message_id')
                if not callback and type(message_id) is int and message_id > 0 and re.fullmatch(r'/[a-z0-9_]{1,32}', command):
                    self.store.enqueue('deleteMessage', {'chat_id': user_id, 'message_id': message_id})
            self.store.set_offset(update_id + 1)

    @contextmanager
    def _transaction(self):
        pending_before = self.pending.copy()
        edits_before = copy.deepcopy(self.faq_edits)
        try:
            with self.store.db:
                yield
        except Exception:
            self.pending = pending_before
            self.faq_edits = edits_before
            raise

    def claim(self, user_id: int, text: str):
        parts = text.split()
        if len(parts) == 2 and self.pairing.redeem(parts[1]):
            previous_operator = self.settings.operator_id
            self.settings.operator_id = user_id
            try:
                self.on_operator(user_id)
            except Exception:
                self.settings.operator_id = previous_operator
                raise
            if previous_operator and previous_operator != user_id:
                self.store.db.execute("DELETE FROM outbox WHERE destination=? AND method='sendMessage'", (previous_operator,))
            self.pending.clear()
            self.faq_edits.clear()
            self.say(user_id, 'Вас підключено як диспетчера. Звернення та відповіді доступні в цьому чаті.', self.menu(user_id))
        else:
            self.say(user_id, 'Код недійсний. Отримайте новий код у вікні програми на комп’ютері.')

    def callback(self, user_id: int, data: str, message_id: int | None = None):
        action, _, value = data.partition(':')
        if message_id is not None and action in ('menu', 'faq', 'faqpage', 'support', 'help', 'privacy', 'identity', 'new',
                                                  'continue', 'back', 'cancel_draft', 'mine', 'admin', 'commands', 'tickets',
                                                  'faqmanage', 'faqedit', 'faqadd', 'faqfield', 'faqremove', 'faqdelete'):
            menu = self.store.menu_message(user_id)
            # Missing persisted identity is not proof that a static button is current.
            if not menu or menu['message_id'] != message_id:
                return
        if action == 'notification_menu':
            action = 'menu'
        if action == 'commands':
            self.operator_commands(user_id)
            return
        edit_actions = ('faqmanage', 'faqedit', 'faqadd', 'faqfield', 'faqremove', 'faqdelete')
        if action not in edit_actions:
            self.faq_edits.pop(user_id, None)
        if action in ('menu', 'faq', 'faqpage', 'support', 'help', 'admin', 'privacy', 'identity', 'mine'):
            self.pending.pop(user_id, None)
        if action == 'menu':
            self.pending.pop(user_id, None)
            self.faq_edits.pop(user_id, None)
            self.say(user_id, 'Оберіть дію:', self.menu(user_id))
        elif action == 'faq' and not value:
            self.show_faq(user_id)
        elif action == 'faq':
            self.show_faq_answer(user_id, value)
        elif action == 'faqpage':
            self.show_faq(user_id, value)
        elif action in edit_actions:
            self.faq_admin_callback(user_id, data)
        elif action == 'admin':
            self.admin(user_id)
        elif action in ('support', 'help'):
            self.support(user_id)
        elif action in ('privacy', 'identity'):
            self.message(user_id, {}, '/privacy' if action == 'privacy' else '/id')
        elif action == 'new':
            self.begin_ticket(user_id)
        elif action == 'continue':
            if self.store.draft(user_id):
                self.prompt_draft(user_id)
            else:
                self.say(user_id, 'Незавершених звернень немає.', self.menu(user_id))
        elif action == 'back':
            self.back_draft(user_id)
        elif action == 'cancel_draft':
            self.cancel_draft(user_id)
        elif action == 'mine':
            ticket = self.store.user_ticket(user_id)
            if ticket:
                self.card(user_id, ticket)
            elif self.store.draft(user_id):
                self.prompt_draft(user_id, 'Звернення ще не зареєстроване: потрібно заповнити всі обов’язкові поля.')
            else:
                self.say(user_id, 'У вас немає відкритих звернень.', self.menu(user_id))
        elif action == 'tickets' and user_id == self.settings.operator_id:
            self.list_tickets(user_id, int(value) if value.isdecimal() and len(value) < 7 else 0)
        elif action in ('ticket', 'reply', 'confirm', 'close') and value.isdecimal() and len(value) < 16:
            ticket = self.store.ticket(int(value))
            if not self._can_access(user_id, ticket):
                self.say(user_id, 'Звернення недоступне або вже закрите.', self.menu(user_id))
                return
            if action == 'ticket':
                self.card(user_id, ticket)
            elif action == 'reply':
                if user_id != self.settings.operator_id:
                    self.say(user_id, 'Ця дія доступна диспетчеру.')
                    return
                self.faq_edits.pop(user_id, None)
                self._pending(user_id, 'reply', ticket['id'])
                self.say(user_id, f"Напишіть відповідь для звернення №{ticket['id']}.",
                         keyboard([[('Скасувати', 'menu')]]), ticket_id=ticket['id'])
            elif action == 'confirm':
                self.say(user_id, f"Закрити звернення №{ticket['id']} і видалити його листування з робочої бази? Уже надіслані електронні листи залишаться у поштовій скриньці.",
                         keyboard([[('Так, закрити', f"close:{ticket['id']}")], [('Скасувати', 'menu')]]), ticket['id'])
            else:
                self.close_ticket(ticket)
        else:
            self.say(user_id, 'Відкрийте меню знову: /start.', self.menu(user_id))

    def operator_commands(self, user_id: int):
        if user_id != self.settings.operator_id:
            self.say(user_id, 'Список команд доступний лише диспетчеру. Дії для клієнтів є в меню та розділі «Підтримка».',
                     self.menu(user_id))
            return
        self.pending.pop(user_id, None)
        self.faq_edits.pop(user_id, None)
        text = ('Команди бота\n\n/start — головне меню\n/faq — поширені запитання\n/support або /help — розділ підтримки\n'
                '/ask — запитання диспетчеру\n/mine — ваше звернення\n/continue — продовжити анкету\n/privacy — про дані\n'
                '/back — попередній крок анкети\n/cancel — скасувати введення та видалити чернетку\n/id — ваш Telegram ID\n'
                '/delete_me — закрити своє звернення й видалити його з робочої бази.\n\n'
                'Дії диспетчера\n/admin — панель\n/tickets — відкриті звернення\n/reply НОМЕР текст — відповідь клієнту\n\n'
                'FAQ редагується в панелі диспетчера. Закриття звернення доступне в його картці.')
        self.say(user_id, text, keyboard([[('Панель диспетчера', 'admin')], [('Головне меню', 'menu')]]))

    def begin_ticket(self, user_id: int):
        if user_id == self.settings.operator_id:
            self.say(user_id, 'Для перевірки ролі клієнта скористайтеся деморежимом або другим Telegram-акаунтом.', self.menu(user_id))
        elif self.store.draft(user_id):
            self.prompt_draft(user_id)
        elif not self.settings.operator_id and not self.settings.mail.enabled:
            self.say(user_id, 'Службу підтримки ще не підключено. Типові відповіді доступні в меню.', self.menu(user_id))
        elif self.store.user_ticket(user_id):
            self.say(user_id, 'Ваше звернення вже відкрите. Наступне текстове повідомлення буде додано до нього.', self.menu(user_id))
        else:
            self._pending(user_id, 'new')
            channels = 'Після заповнення дані будуть передані на електронну пошту служби підтримки.' if self.settings.mail.enabled else 'Після заповнення дані отримає диспетчер.'
            self.say(user_id, 'Опишіть запитання одним текстовим повідомленням, до 2000 одиниць UTF-16. Потім обов’язково вкажіть email, телефон, ПІБ і компанію. ' + channels,
                     keyboard([[('Скасувати', 'menu')]]))

    def prompt_draft(self, user_id: int, prefix: str = ''):
        draft = self.store.draft(user_id)
        if not draft:
            return
        rows = [[('Назад', 'back')]] if draft['step'] != 'email' else []
        rows.append([('Скасувати чернетку', 'cancel_draft')])
        text = (prefix + '\n\n' if prefix else '') + PROMPTS[draft['step']]
        self.say(user_id, text, keyboard(rows), draft_user_id=user_id)

    def back_draft(self, user_id: int):
        draft = self.store.draft(user_id)
        if not draft:
            self.say(user_id, 'Немає незавершеної анкети.', self.menu(user_id))
            return
        index = STEPS.index(draft['step'])
        if index:
            self.store.update_draft(user_id, step=STEPS[index - 1])
        self.prompt_draft(user_id)

    def cancel_draft(self, user_id: int):
        self.store.cancel_draft(user_id, self.settings.delete_telegram_on_close)
        self.pending.pop(user_id, None)
        self.say(user_id, 'Введення скасовано. Чернетку видалено з робочої бази.', self.menu(user_id))

    def advance_draft(self, user_id: int, text: str, message: dict):
        draft = self.store.draft(user_id)
        self.store.track_draft(user_id, message['message_id'], message.get('date', time.time()))
        try:
            values = validate_step(draft['step'], text)
        except ValueError as error:
            self.prompt_draft(user_id, str(error))
            return
        self.store.update_draft(user_id, **values)
        index = STEPS.index(draft['step'])
        if index + 1 < len(STEPS):
            self.store.update_draft(user_id, step=STEPS[index + 1])
            self.prompt_draft(user_id)
        else:
            self.submit_draft(user_id)

    def submit_draft(self, user_id: int):
        draft = self.store.draft(user_id)
        if not contacts_complete(draft):
            self.prompt_draft(user_id, 'Заповніть усі обов’язкові поля.')
            return
        if not self.settings.operator_id and not self.settings.mail.enabled:
            self.prompt_draft(user_id, 'Підтримка тимчасово не підключена. Дані збережено; спробуйте завершити пізніше.')
            return
        ticket_id = draft['ticket_id']
        if not ticket_id:
            ticket_id = self.store.create_ticket(user_id, draft['telegram_name'], draft['telegram_username'])
        self.store.set_contacts(ticket_id, draft)
        references = json.loads(draft['message_refs'])
        question = draft['question']
        self.store.delete_draft(user_id)
        self.accept_question(user_id, ticket_id, question, references, show_contacts=True)

    @staticmethod
    def contact_summary(ticket) -> str:
        if not contacts_complete(ticket):
            return 'Контактні дані ще не заповнено.'
        return (f"ПІБ: {ticket['client_full_name']}\nКомпанія: {ticket['client_company']}\n"
                f"Email: {ticket['client_email']}\nТелефон: {ticket['client_phone']}")

    def accept_question(self, user_id: int, ticket_id: int, text: str, references: list[dict], show_contacts: bool = False):
        ticket = self.store.ticket(ticket_id)
        if not contacts_complete(ticket):
            raise ValueError('Спочатку потрібно заповнити всі контактні дані клієнта')
        row_id = self.store.add_message(ticket_id, 'client', text)
        for item in references:
            self.store.track(ticket_id, user_id, item['id'], item['date'])
        if self.settings.mail.enabled:
            self.queue_mail(ticket_id, row_id, text)
            confirmation = f'Ваше повідомлення прийнято у зверненні №{ticket_id}. Лист службі підтримки поставлено в чергу надсилання.'
        else:
            confirmation = f'Повідомлення збережено у зверненні №{ticket_id} і поставлено в чергу диспетчеру. Поштову пересилку ще не ввімкнено.'
        if show_contacts:
            confirmation += '\n\n' + self.contact_summary(ticket)
        self.say(user_id, confirmation, self.menu(user_id), ticket_id)
        if self.settings.operator_id:
            self.say(self.settings.operator_id, f'Звернення №{ticket_id}\n\n' + self.contact_summary(ticket) + '\n\n' + text,
                     keyboard([[('Відповісти', f'reply:{ticket_id}'), ('Картка', f'ticket:{ticket_id}')],
                               [('Закрити', f'confirm:{ticket_id}')]]), ticket_id, notification=True)

    def message(self, user_id: int, message: dict, command: str):
        text = message.get('text', '').strip()
        if command == '/commands':
            self.operator_commands(user_id)
            return
        if command and command != '/cancel':
            self.faq_edits.pop(user_id, None)
        if command in ('/support', '/help', '/faq', '/admin', '/mine', '/privacy', '/id'):
            self.pending.pop(user_id, None)
        if command == '/cancel':
            if self.faq_edits.pop(user_id, None):
                self.admin(user_id, 'Редагування FAQ скасовано.\n\n')
                return
            self.cancel_draft(user_id)
            return
        if command == '/back':
            self.back_draft(user_id)
            return
        if command in ('/start', '/menu'):
            self.pending.pop(user_id, None)
            self.faq_edits.pop(user_id, None)
            self.say(user_id, 'Вітаємо! Я — інформаційний бот про гранти на обладнання для переробних підприємств у межах програми «Зроблено в Україні». '
                     'Допоможу дізнатися про умови участі, фінансування, подання заявки та використання коштів.\n\nОберіть розділ:', self.menu(user_id))
            return
        if command in ('/help', '/support'):
            self.support(user_id)
            return
        if command == '/faq':
            self.show_faq(user_id)
            return
        if command == '/admin':
            self.admin(user_id)
            return
        if command == '/continue':
            self.callback(user_id, 'continue')
            return
        if command == '/privacy':
            self.say(user_id, 'Бот зберігає запитання, Telegram ID, дані профілю та обов’язкові контакти: email, телефон, ПІБ і компанію. Чернетка зберігається на комп’ютері власника до завершення або скасування. '
                     'Для оновлення меню зберігається ідентифікатор його повідомлення. '
                     'Після заповнення всіх полів ці дані передаються диспетчеру та, якщо пересилку ввімкнено, на пошту підтримки. '
                     'Після закриття листування видаляється з робочої бази; надіслані електронні листи та ручні резервні копії залишаються. '
                     'Видалення повідомлень у Telegram виконується за можливості; видалити весь діалог бот не може. '
                     'Паролі та коди входу не надсилайте.', self.menu(user_id))
            return
        if command == '/id':
            self.say(user_id, f'Ваш Telegram ID: {user_id}')
            return
        if command == '/ask':
            self.begin_ticket(user_id)
            return
        if command == '/mine':
            self.callback(user_id, 'mine')
            return
        if command == '/delete_me':
            ticket = self.store.user_ticket(user_id)
            self.pending.pop(user_id, None)
            if ticket:
                self.close_ticket(ticket)
            self.store.cancel_draft(user_id, self.settings.delete_telegram_on_close)
            if not ticket:
                self.say(user_id, 'Ваші незавершені звернення видалено з робочої бази.', self.menu(user_id))
            return
        if command == '/tickets' and user_id == self.settings.operator_id:
            self.list_tickets(user_id)
            return
        if command == '/reply' and user_id == self.settings.operator_id:
            self.faq_edits.pop(user_id, None)
            parts = text.split(maxsplit=2)
            if len(parts) == 3 and parts[1].isdecimal() and len(parts[1]) < 16:
                self.reply(user_id, int(parts[1]), parts[2], message)
            else:
                self.say(user_id, 'Формат: /reply НОМЕР текст відповіді')
            return
        if command:
            self.say(user_id, 'Невідома команда. Відкрити меню: /start.', self.menu(user_id))
            return
        if not text:
            self.say(user_id, 'Поки що приймаються лише текстові повідомлення. Опишіть запитання текстом.')
            return
        if self.faq_admin_message(user_id, text):
            return
        if self.store.draft(user_id) and user_id != self.settings.operator_id:
            self.advance_draft(user_id, text, message)
            return
        if text_size(text) > 2000:
            self.say(user_id, 'Завелике повідомлення: максимум 2000 одиниць UTF-16. Розділіть його на частини.')
            return
        pending = self.pending.get(user_id)
        if pending and pending[2] < time.monotonic():
            self.pending.pop(user_id, None)
            pending = None
        if user_id == self.settings.operator_id:
            if pending and pending[0] == 'reply':
                if self.reply(user_id, pending[1], text, message):
                    self.pending.pop(user_id, None)
            else:
                self.say(user_id, 'Оберіть дію в панелі диспетчера. Для відповіді відкрийте звернення та натисніть «Відповісти».', self.menu(user_id))
            return
        ticket = self.store.user_ticket(user_id)
        if not ticket and not (pending and pending[0] == 'new'):
            self.say(user_id, 'Щоб надіслати звернення, натисніть «Запитання диспетчеру».', self.menu(user_id))
            return
        if not self.settings.operator_id and not self.settings.mail.enabled:
            self.say(user_id, 'Служба підтримки поки що недоступна.', self.menu(user_id))
            return
        actor = message.get('from', {})
        name = ' '.join(str(actor.get(key, '')).replace('\n', ' ').replace('\r', ' ') for key in ('first_name', 'last_name')).strip()[:200]
        username = str(actor.get('username', ''))
        username = username if re.fullmatch(r'[A-Za-z0-9_]{1,32}', username) else ''
        if not ticket or not contacts_complete(ticket):
            self.store.create_draft(user_id, text, name, username, message['message_id'], message.get('date', time.time()),
                                    ticket['id'] if ticket else None)
            self.pending.pop(user_id, None)
            self.prompt_draft(user_id, 'Запитання збережено як чернетку. Для реєстрації звернення обов’язково заповніть усі 4 поля. До завершення анкети запитання не передається підтримці.')
        else:
            self.accept_question(user_id, ticket['id'], text, [{'id': message['message_id'], 'date': message.get('date', time.time())}])

    def queue_mail(self, ticket_id: int, message_row_id: int, question: str):
        ticket = self.store.ticket(ticket_id)
        if not contacts_complete(ticket):
            raise ValueError('Для надсилання потрібні email, телефон, ПІБ і компанія клієнта')
        number = self.store.message_count(ticket_id)
        accepted = time.time()
        fields = {
            'ticket_id': ticket_id, 'message_number': number,
            'event_label': 'Нове звернення' if number == 1 else 'Доповнення до звернення',
            'accepted_at': datetime.fromtimestamp(accepted, timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC'),
            'client_id': ticket['user_id'], 'client_name': ticket['client_name'] or 'Ім’я не вказане',
            'client_username': '@' + ticket['client_username'] if ticket['client_username'] else 'Не вказане',
            'client_link': 'https://t.me/' + ticket['client_username'] if ticket['client_username'] else f"tg://user?id={ticket['user_id']}",
            'question': question, 'bot_username': self.bot_username,
            'client_email': ticket['client_email'], 'client_phone': ticket['client_phone'],
            'client_full_name': ticket['client_full_name'], 'client_first_name': ticket['client_first_name'],
            'client_company': ticket['client_company'],
        }
        payload = self.mail_template.render(fields)
        domain = self.settings.mail.sender.rpartition('@')[2] or 'infobot.local'
        message_id = '<infobot-' + uuid.uuid4().hex + '@' + domain + '>'
        root_id = ticket['mail_thread_id']
        if not root_id:
            self.store.db.execute('UPDATE tickets SET mail_thread_id=? WHERE id=?', (message_id, ticket_id))
        payload.update(message_id=message_id, in_reply_to=root_id, accepted_epoch=accepted, client_email=ticket['client_email'])
        self.store.enqueue_mail(ticket_id, message_row_id, payload)

    def reply(self, user_id: int, ticket_id: int, text: str, message: dict) -> bool:
        if user_id != self.settings.operator_id:
            return False
        ticket = self.store.ticket(ticket_id)
        if not ticket:
            self.say(user_id, 'Звернення вже закрите або не знайдене.', self.menu(user_id))
            return True
        if not text.strip() or text_size(text) > 2000:
            self.say(user_id, 'Відповідь має містити від 1 до 2000 одиниць UTF-16.')
            return False
        self.store.add_message(ticket_id, 'operator', text)
        self.store.track(ticket_id, user_id, message['message_id'], message.get('date'))
        self.say(ticket['user_id'], f'Відповідь диспетчера · звернення №{ticket_id}\n\n{text}',
                 keyboard([[('Моє звернення', 'mine')]]), ticket_id, notification=True)
        self.say(user_id, f'Відповідь для звернення №{ticket_id} поставлено в чергу надсилання.', self.menu(user_id), ticket_id)
        return True

    def card(self, user_id: int, ticket):
        details = [f"Звернення №{ticket['id']} · відкрите", self.contact_summary(ticket)]
        pending, sent, blocked = self.store.mail_counts(ticket['id'])
        if pending or sent or blocked:
            details.append(f'Електронна пошта: у черзі — {pending}; передано SMTP — {sent}; потребують перевірки — {blocked}.')
        elif not self.settings.mail.enabled:
            details.append('Поштову пересилку не ввімкнено.')
        for row in self.store.history(ticket['id']):
            who = 'Клієнт' if row['direction'] == 'client' else 'Диспетчер'
            details.append(who + ': ' + shorten(row['body']))
        rows = []
        if user_id == self.settings.operator_id:
            rows.append([('Відповісти', f"reply:{ticket['id']}")])
        rows.extend([[('Закрити звернення', f"confirm:{ticket['id']}")], [('До меню', 'menu')]])
        self.say(user_id, '\n\n'.join(details), keyboard(rows), ticket['id'])

    def list_tickets(self, user_id: int, page: int = 0):
        if user_id != self.settings.operator_id:
            return
        count = self.store.counts()[0]
        page = min(max(page, 0), max(0, (count - 1) // 8))
        rows = [[(f"Звернення №{ticket['id']}", f"ticket:{ticket['id']}")] for ticket in self.store.tickets(page)]
        navigation = []
        if page:
            navigation.append(('Назад', f'tickets:{page - 1}'))
        if (page + 1) * 8 < count:
            navigation.append(('Далі', f'tickets:{page + 1}'))
        if navigation:
            rows.append(navigation)
        rows.append([('До меню', 'menu')])
        self.say(user_id, f'Відкритих звернень: {count}. Сторінка {page + 1}.', keyboard(rows))

    def close_ticket(self, ticket):
        ticket_id, user_id = ticket['id'], ticket['user_id']
        draft = self.store.draft(user_id)
        first_name = ticket['client_first_name'] or (draft['client_first_name'] if draft else '')
        if not first_name and ticket['client_name']:
            first_name = ticket['client_name'].split()[0]
        for chat_id in self.store.purge_ticket(ticket_id, self.settings.delete_telegram_on_close):
            self.say(chat_id, 'Оберіть дію.', self.menu(chat_id))
        for actor, state in list(self.pending.items()):
            if actor == user_id or state[1] == ticket_id:
                self.pending.pop(actor, None)
        greeting = address_name(first_name) + ', ' if first_name else ''
        text = greeting + f'дякуємо за звернення! Ваше звернення №{ticket_id} закрите. Листування видалено з робочої бази. Уже надіслані електронні листи залишаються у служби підтримки.'
        markup = keyboard([[('Головне меню', 'notification_menu')]])
        self.say(user_id, text, markup, notification=True)
        if self.settings.operator_id:
            self.say(self.settings.operator_id, f'Звернення №{ticket_id} закрите. Листування видалено з робочої бази.',
                     markup, notification=True)
