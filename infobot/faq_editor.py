from __future__ import annotations

import tkinter as tk
import uuid
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .faq import FAQCatalog, FAQConflict


class FAQEditor(ttk.Frame):
    def __init__(self, master, catalog: FAQCatalog):
        super().__init__(master, padding=16)
        self.catalog = catalog
        self.items = []
        self.selected_id = None
        self.loaded_revision = -1
        self.question = tk.StringVar()
        self.source = tk.StringVar()
        self.status = tk.StringVar()
        ttk.Label(self, text='FAQ для клієнтів', font=('Segoe UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(self, text='Зміни одразу з’являються в Telegram. Диспетчер також може редагувати FAQ у чаті бота.',
                  wraplength=850).pack(anchor='w', pady=(6, 12))
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        self.list = tk.Listbox(left, exportselection=False, font=('Segoe UI', 10), width=32)
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=self.list.yview)
        self.list.configure(yscrollcommand=scrollbar.set)
        self.list.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.list.bind('<<ListboxSelect>>', self.select)
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew')
        ttk.Label(right, text='Запитання (один рядок, до 60 одиниць UTF-16):').pack(anchor='w')
        ttk.Entry(right, textvariable=self.question).pack(fill='x', pady=(4, 10))
        ttk.Label(right, text='Проста відповідь для клієнта (до 3000 одиниць UTF-16):').pack(anchor='w')
        self.answer = ScrolledText(right, height=12, wrap='word', font=('Segoe UI', 11))
        self.answer.pack(fill='both', expand=True, pady=(4, 10))
        ttk.Label(right, text='Посилання на джерело (HTTPS, можна залишити порожнім):').pack(anchor='w')
        ttk.Entry(right, textvariable=self.source).pack(fill='x', pady=(4, 10))
        actions = ttk.Frame(self)
        actions.pack(fill='x', pady=(14, 8))
        for title, command in (('Додати запитання', self.new), ('Зберегти', self.save),
                               ('Видалити', self.delete), ('Оновити список', self.refresh)):
            ttk.Button(actions, text=title, command=command).pack(side='left', padx=(0, 8))
        ttk.Label(self, textvariable=self.status, wraplength=850).pack(anchor='w')
        ttk.Label(self, text='Після ручної зміни відповіді позначка перевірки джерела прибирається. '
                  'Перевіряйте умови програми перед публікацією. У деморежимі зміни FAQ діють лише до «Почати знову».',
                  wraplength=850).pack(anchor='w', pady=(8, 0))
        self.refresh()
        self.after(800, self.watch)

    def refresh(self):
        try:
            self.catalog.reload()
            self.items, self.loaded_revision = self.catalog.snapshot()
        except (ValueError, OSError, RuntimeError) as error:
            self.error(error)
            return
        self.list.delete(0, 'end')
        for item in self.items:
            self.list.insert('end', item['question'])
        index = next((i for i, item in enumerate(self.items) if item['id'] == self.selected_id), 0)
        self.list.selection_set(index)
        self.list.see(index)
        self.select()
        self.status.set(f'Запитань: {len(self.items)}. Збережено на цьому комп’ютері; перезапуск не потрібен.')

    def select(self, event=None):
        indices = self.list.curselection()
        if not indices:
            return
        item = self.items[indices[0]]
        self.selected_id = item['id']
        self.question.set(item['question'])
        self.source.set(item.get('source_url', ''))
        self.answer.delete('1.0', 'end')
        self.answer.insert('1.0', item['answer'])

    def new(self):
        self.selected_id = None
        self.list.selection_clear(0, 'end')
        self.question.set('')
        self.source.set('')
        self.answer.delete('1.0', 'end')
        self.status.set('Нове запитання: заповніть поля та натисніть «Зберегти».')

    def save(self):
        values = {'question': self.question.get(), 'answer': self.answer.get('1.0', 'end-1c'),
                  'source_url': self.source.get()}
        try:
            if self.selected_id:
                self.catalog.update(self.selected_id, values, self.loaded_revision)
            else:
                identifier = uuid.uuid4().hex[:20]
                self.catalog.add({'id': identifier, **values}, self.loaded_revision)
                self.selected_id = identifier
        except (ValueError, OSError, RuntimeError) as error:
            self.error(error)
            return
        self.refresh()
        self.status.set('FAQ збережено. Нові відповіді вже доступні клієнтам.')

    def delete(self):
        if not self.selected_id:
            return
        if not messagebox.askyesno('Видалення запитання', 'Видалити вибране запитання з FAQ?', parent=self):
            return
        try:
            self.catalog.delete(self.selected_id, self.loaded_revision)
        except (ValueError, OSError, RuntimeError) as error:
            self.error(error)
            return
        self.selected_id = None
        self.refresh()

    def error(self, error):
        text = str(error) if isinstance(error, ValueError) else 'Перевірте доступ на запис до папки data.'
        if isinstance(error, FAQConflict):
            text += '\nСкопіюйте свій текст, натисніть «Оновити список» і повторіть збереження.'
        messagebox.showerror('FAQ не збережено', text, parent=self)

    def watch(self):
        if self.catalog.revision != self.loaded_revision:
            self.status.set('FAQ змінено в Telegram. Натисніть «Оновити список» перед наступним редагуванням. Ваш текст у полях збережено.')
        self.after(800, self.watch)
