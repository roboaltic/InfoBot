from __future__ import annotations

import queue
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .faq import FAQCatalog
from .mailer import MailTemplate
from .runner import Runner
from .settings import ROOT, MailSettings, Settings, load_secret, load_token, save_secret, save_token


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.runner = None
        self.events = queue.Queue()
        self.username = ''
        self.had_error = self.closing = False
        self.form_widgets = []
        self.startup_error = ''
        self.faq_catalog = FAQCatalog.open(ROOT)
        try:
            self.settings = Settings.load(ROOT / 'data' / 'config.json')
            token = load_token(ROOT / 'data' / 'token.secret')
            password = load_secret(ROOT / 'data' / 'smtp.secret', 'INFOBOT_SMTP_PASSWORD')
        except (ValueError, OSError):
            self.settings, token, password = Settings(), '', ''
            self.startup_error = 'Не вдалося прочитати налаштування або облікові дані. Перевірте data/config.json; секрети можна ввести знову.'
        root.title(f'InfoBot {__version__} · локальний запуск')
        root.geometry('1000x820')
        root.minsize(900, 760)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        for name in ('TFrame', 'TLabel', 'TNotebook'):
            style.configure(name, background='#f4f6fa')
        style.configure('TButton', padding=(10, 6))
        style.configure('Title.TLabel', font=('Segoe UI', 23, 'bold'), foreground='#132846')
        style.configure('Status.TLabel', font=('Segoe UI', 11, 'bold'), foreground='#1b5e69')
        root.configure(bg='#f4f6fa')
        header = ttk.Frame(root, padding=(22, 18, 22, 10))
        header.pack(fill='x')
        ttk.Label(header, text='InfoBot', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='Звернення · обов’язкові контакти · електронна пошта', font=('Segoe UI', 11)).pack(side='left', padx=18)
        notebook = ttk.Notebook(root)
        notebook.pack(fill='both', expand=True, padx=18, pady=(0, 18))
        launch = ttk.Frame(notebook, padding=18)
        notebook.add(launch, text='  Запуск  ')
        mail_tab = ttk.Frame(notebook, padding=18)
        notebook.add(mail_tab, text='  Електронна пошта  ')
        connections_tab = ttk.Frame(notebook, padding=18)
        notebook.add(connections_tab, text='  Підключення  ')
        ttk.Label(connections_tab, text='Користувачі, які звернулися до бота. Час першого підключення за місцевим часом комп’ютера.',
                  wraplength=850).pack(anchor='w', pady=(0, 12))
        table_frame = ttk.Frame(connections_tab)
        table_frame.pack(fill='both', expand=True)
        self.connections_table = ttk.Treeview(table_frame, columns=('id', 'name', 'time'), show='headings')
        for column, title, width in (('id', 'Telegram ID', 160), ('name', 'Ім’я користувача', 360),
                                     ('time', 'Час підключення', 220)):
            self.connections_table.heading(column, text=title)
            self.connections_table.column(column, width=width, minwidth=100)
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.connections_table.yview)
        self.connections_table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.connections_table.pack(side='left', fill='both', expand=True)
        self.connection_rows = None
        self.token = tk.StringVar(value=token)
        self.allowlist = tk.StringVar(value=', '.join(map(str, self.settings.allowed_user_ids)))
        self.delete_messages = tk.BooleanVar(value=self.settings.delete_telegram_on_close)
        self.status = tk.StringVar(value='Зупинено')
        self.counts = tk.StringVar(value='Відкритих звернень: —  |  У черзі Telegram: —')
        self.mail_counts = tk.StringVar(value='Листи: у черзі —; передано SMTP —; потребують перевірки —')
        self.operator = tk.StringVar(value=f"Спеціаліст: {self.settings.operator_id or 'не підключений'}")
        self.claim = tk.StringVar()
        ttk.Label(launch, text='Токен BotFather (вводиться лише на цьому комп’ютері):').pack(anchor='w')
        self.token_entry = ttk.Entry(launch, textvariable=self.token, font=('Consolas', 11))
        self.token_entry.pack(fill='x', pady=(5, 10))
        self.form_widgets.append((self.token_entry, 'normal'))
        ttk.Label(launch, text='Учасники тесту: Telegram ID через кому; порожнє поле — доступно всім, хто знайде бота.').pack(anchor='w')
        entry = ttk.Entry(launch, textvariable=self.allowlist)
        entry.pack(fill='x', pady=(5, 8))
        self.form_widgets.append((entry, 'normal'))
        check = ttk.Checkbutton(launch, text='Після закриття пробувати видаляти повідомлення звернення в Telegram', variable=self.delete_messages)
        check.pack(anchor='w', pady=(0, 12))
        self.form_widgets.append((check, 'normal'))
        actions = ttk.Frame(launch)
        actions.pack(fill='x')
        self.start_button = ttk.Button(actions, text='Зберегти й запустити', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='Зупинити', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        self.open_button = ttk.Button(actions, text='Відкрити бота', command=self.open_bot, state='disabled')
        self.open_button.pack(side='left')
        ttk.Label(launch, textvariable=self.status, style='Status.TLabel', wraplength=860).pack(anchor='w', pady=(14, 4))
        ttk.Label(launch, textvariable=self.counts).pack(anchor='w')
        ttk.Label(launch, textvariable=self.mail_counts).pack(anchor='w', pady=(4, 0))
        pairing = ttk.LabelFrame(launch, text='Спеціаліст', padding=10)
        pairing.pack(fill='x', pady=14)
        ttk.Label(pairing, textvariable=self.operator).pack(anchor='w')
        row = ttk.Frame(pairing)
        row.pack(fill='x', pady=(6, 0))
        self.pair_button = ttk.Button(row, text='Отримати новий код', command=lambda: self.control('pairing'), state='disabled')
        self.pair_button.pack(side='left')
        ttk.Entry(row, textvariable=self.claim, state='readonly', font=('Consolas', 10)).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(row, text='Копіювати', command=self.copy_claim).pack(side='right')
        ttk.Label(pairing, text='Надішліть команду з поля своєму боту в Telegram. Код одноразовий, діє 10 хвилин.', wraplength=820).pack(anchor='w', pady=(8, 0))
        ttk.Label(launch, text='Бот працює, доки цей комп’ютер увімкнений, підключений до інтернету, а програма відкрита.',
                  wraplength=860).pack(anchor='w', pady=(0, 10))
        ttk.Label(launch, text='Журнал роботи (без листування, токена та поштового пароля):').pack(anchor='w')
        self.log_view = ScrolledText(launch, height=7, state='disabled', wrap='word', font=('Consolas', 10), relief='flat')
        self.log_view.pack(fill='both', expand=True, pady=(5, 0))
        self.build_mail_tab(mail_tab, password)
        if self.startup_error:
            self.log(self.startup_error)
        root.after(100, self.pump)

    def build_mail_tab(self, parent, password):
        self.mail_enabled = tk.BooleanVar(value=self.settings.mail.enabled)
        self.mail_password = tk.StringVar(value=password)
        self.mail_vars = {key: tk.StringVar(value=str(getattr(self.settings.mail, key)))
                          for key in ('host', 'port', 'security', 'username', 'sender', 'recipient')}
        check = ttk.Checkbutton(parent, text='Автоматично надсилати прийняті запитання та доповнення на пошту', variable=self.mail_enabled)
        check.pack(anchor='w', pady=(0, 12))
        self.form_widgets.append((check, 'normal'))
        ttk.Label(parent, text='Поки скриньку не створено, залиште пересилання вимкненим. Адресу отримувача задає власник; клієнт її не змінює.',
                  wraplength=850).pack(anchor='w', pady=(0, 14))
        form = ttk.Frame(parent)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        labels = {'host': 'SMTP-сервер', 'port': 'SMTP-порт', 'security': 'Захист з’єднання',
                  'username': 'Логін поштової скриньки', 'sender': 'Адреса відправника', 'recipient': 'Адреса отримувача (підтримка)'}
        for row, key in enumerate(labels):
            ttk.Label(form, text=labels[key] + ':').grid(row=row, column=0, sticky='w', padx=(0, 16), pady=6)
            if key == 'security':
                widget = ttk.Combobox(form, textvariable=self.mail_vars[key], values=('ssl', 'starttls'), state='readonly')
                widget.bind('<<ComboboxSelected>>', self.suggest_port)
                normal = 'readonly'
            else:
                widget = ttk.Entry(form, textvariable=self.mail_vars[key])
                normal = 'normal'
            widget.grid(row=row, column=1, sticky='ew', pady=6)
            self.form_widgets.append((widget, normal))
        ttk.Label(form, text='Пароль застосунку для SMTP:').grid(row=6, column=0, sticky='w', padx=(0, 16), pady=6)
        entry = ttk.Entry(form, textvariable=self.mail_password, show='*')
        entry.grid(row=6, column=1, sticky='ew', pady=6)
        self.form_widgets.append((entry, 'normal'))
        ttk.Label(parent, text='Типові варіанти: ssl — TLS від початку з’єднання, часто порт 465; starttls — перехід на TLS, часто порт 587. Точні параметри візьміть у свого поштового сервісу. Пароль у Windows захищається для поточного користувача й не потрапляє в config.json або резервну копію бази.',
                  wraplength=850).pack(anchor='w', pady=(12, 14))
        actions = ttk.Frame(parent)
        actions.pack(fill='x')
        button = ttk.Button(actions, text='Зберегти налаштування', command=self.save_mail)
        button.pack(side='left')
        self.form_widgets.append((button, 'normal'))
        self.retry_button = ttk.Button(actions, text='Повторити листи з черги', command=lambda: self.control('retry_mail'), state='disabled')
        self.retry_button.pack(side='right')
        ttk.Label(parent, text='Після зміни параметрів перезапустіть бота. Статус «передано SMTP» означає прийняття листа поштовим сервером; потрапляння у вхідні залежить від поштового сервісу. Відповіді на листи автоматично клієнту в Telegram не пересилаються.',
                  wraplength=850).pack(anchor='w', pady=(16, 0))

    def suggest_port(self, event=None):
        if self.mail_vars['port'].get() in ('465', '587'):
            self.mail_vars['port'].set('465' if self.mail_vars['security'].get() == 'ssl' else '587')

    def collect_settings(self):
        result = Settings.load(ROOT / 'data' / 'config.json')
        raw = self.allowlist.get().replace(';', ',').replace(' ', ',')
        try:
            identifiers = [int(part) for part in raw.split(',') if part.strip()]
            port = int(self.mail_vars['port'].get())
        except ValueError:
            raise ValueError('Telegram ID та SMTP-порт мають бути цілими числами') from None
        if any(user_id <= 0 for user_id in identifiers):
            raise ValueError('Telegram ID мають бути додатними числами')
        result.allowed_user_ids = sorted(set(identifiers))
        result.delete_telegram_on_close = self.delete_messages.get()
        values = {key: variable.get().strip() for key, variable in self.mail_vars.items() if key != 'port'}
        result.mail = MailSettings(enabled=self.mail_enabled.get(), port=port, **values)
        result.mail.validate()
        if result.mail.enabled and not self.mail_password.get():
            raise ValueError('Укажіть пароль застосунку для SMTP або залиште поштову пересилку вимкненою')
        MailTemplate.load(ROOT / 'mail_template.json')
        return result

    def save_settings(self, require_token=False):
        self.settings = self.collect_settings()
        token = self.token.get().strip()
        if require_token or token:
            save_token(ROOT / 'data' / 'token.secret', token)
        if self.mail_password.get():
            save_secret(ROOT / 'data' / 'smtp.secret', self.mail_password.get())
        self.settings.save(ROOT / 'data' / 'config.json')

    def save_mail(self):
        try:
            self.save_settings()
        except (ValueError, OSError) as error:
            self.settings_error(error)
            return
        messagebox.showinfo('Налаштування збережено', 'Поштові налаштування збережено. Справжній лист під час збереження не надсилається. Запустіть бота для автоматичного пересилання нових запитань.')

    def settings_error(self, error):
        details = str(error) if isinstance(error, ValueError) else 'Потрібен доступ на запис у папку програми.'
        messagebox.showerror('Налаштування', 'Не вдалося зберегти або перевірити налаштування.\n' + details)

    def log(self, text):
        self.log_view.configure(state='normal')
        self.log_view.insert('end', datetime.now().strftime('%H:%M:%S') + '  ' + str(text) + '\n')
        if int(self.log_view.index('end-1c').split('.')[0]) > 500:
            self.log_view.delete('1.0', '100.0')
        self.log_view.configure(state='disabled')
        self.log_view.see('end')

    def start(self):
        if self.runner and self.runner.thread.is_alive():
            return
        try:
            self.save_settings(require_token=True)
        except (ValueError, OSError) as error:
            self.settings_error(error)
            return
        self.had_error = False
        self.claim.set('')
        self.username = ''
        self.status.set('Підключення до Telegram…')
        self.start_button.configure(state='disabled')
        self.set_forms_enabled(False)
        self.stop_button.configure(state='normal')
        self.runner = Runner(self.token.get().strip(), self.settings, lambda kind, value: self.events.put((kind, value)), faq_catalog=self.faq_catalog)
        self.runner.start()

    def set_forms_enabled(self, enabled):
        for widget, normal in self.form_widgets:
            widget.configure(state=normal if enabled else 'disabled')

    def stop(self):
        if self.runner:
            self.runner.stop.set()
            if self.runner.mail_worker:
                self.runner.mail_worker.stop.set()
            self.status.set('Зупинка… Дочекайтеся завершення поточних запитів Telegram і SMTP.')
            self.stop_button.configure(state='disabled')

    def control(self, action):
        if self.runner and self.runner.thread.is_alive():
            self.runner.controls.put(action)

    def copy_claim(self):
        if self.claim.get():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.claim.get())

    def open_bot(self):
        if self.username:
            webbrowser.open('https://t.me/' + self.username)

    def show_connections(self, rows):
        if rows == self.connection_rows:
            return
        self.connection_rows = rows
        children = self.connections_table.get_children()
        if children:
            self.connections_table.delete(*children)
        for row in rows:
            name = row['name']
            if row['username']:
                name = (name + ' ' if name else '') + '@' + row['username']
            connected = datetime.fromtimestamp(row['connected_at']).astimezone().strftime('%d.%m.%Y %H:%M:%S %Z')
            self.connections_table.insert('', 'end', values=(row['user_id'], name or '—', connected))

    def pump(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.log(value)
            elif kind == 'running':
                self.username = value
                self.status.set('Працює · @' + value)
                for button in (self.open_button, self.pair_button):
                    button.configure(state='normal')
                self.retry_button.configure(state='normal' if self.settings.mail.enabled else 'disabled')
            elif kind == 'pairing':
                self.claim.set('/claim ' + value)
                self.log('Код підключення спеціаліста оновлено. Команду показано в окремому полі.')
            elif kind == 'operator':
                self.operator.set(f'Спеціаліст: {value}')
                self.claim.set('')
                self.log('Спеціаліста підключено.')
            elif kind == 'connections':
                self.show_connections(value)
            elif kind == 'counts':
                self.counts.set(f'Відкритих звернень: {value[0]}  |  У черзі Telegram: {value[1]}')
            elif kind == 'mail_counts':
                self.mail_counts.set(f'Листи: у черзі {value[0]}; передано SMTP {value[1]}; потребують перевірки {value[2]}')
            elif kind == 'backup':
                self.log('Резервну копію створено: ' + value)
                messagebox.showinfo('Резервна копія', 'Створено незашифровану копію звернень, контактних анкет, чернеток і черг. Каталог FAQ зберігається окремим файлом поруч:\n' + value + '\n\nТокен і поштовий пароль у копію не входять.')
            elif kind == 'error':
                self.had_error = True
                self.status.set('Помилка · див. журнал')
                self.log(value)
            elif kind == 'stopped':
                if not self.had_error:
                    self.status.set('Зупинено')
                self.claim.set('')
                for button in (self.stop_button, self.open_button, self.pair_button, self.retry_button):
                    button.configure(state='disabled')
                self.start_button.configure(state='normal')
                self.set_forms_enabled(True)
                self.log('Бота зупинено. Ненадіслані повідомлення та листи збережені в чергах.')
        if self.closing and (not self.runner or not self.runner.thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.pump)

    def close(self):
        self.closing = True
        self.stop()
        if not self.runner or not self.runner.thread.is_alive():
            self.root.destroy()


def launch(demo_first: bool = False):
    root = tk.Tk()
    # Keep the old launcher argument compatible; always show server controls.
    App(root)
    root.mainloop()
