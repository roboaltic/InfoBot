"""Tk widgets stay on the main thread; commands and snapshots cross queues only."""
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

SUCCESS = 'Повідомлення надіслано клієнту. Подальші відповіді клієнта надходитимуть у цей чат звернення.'


class Chats(ttk.Frame):
    def __init__(self, parent, command):
        super().__init__(parent, padding=12)
        self.command = command
        self.rows = {}
        self.tabs = {}
        self.active = None
        self.label = tk.StringVar(value='Чати · 0')
        ttk.Button(self, textvariable=self.label, command=self.show_list).pack(anchor='w')
        self.book = ttk.Notebook(self)
        self.book.pack(fill='both', expand=True, pady=8)
        self.list_frame = ttk.Frame(self.book)
        self.book.add(self.list_frame, text='Список звернень')
        self.table = ttk.Treeview(self.list_frame, columns=('number', 'name', 'state', 'last', 'unread'), show='headings')
        for key, title in zip(self.table['columns'], ('Номер', 'Клієнт', 'Стан', 'Останнє повідомлення', 'Непрочитані')):
            self.table.heading(key, text=title)
            self.table.column(key, width=140, minwidth=60)
        scroll = ttk.Scrollbar(self.list_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<Double-1>', self.open_selected)
        ttk.Button(self.list_frame, text='Відкрити чат', command=self.open_selected).pack()
        self.book.bind('<<NotebookTabChanged>>', self.selected)

    def open_selected(self, event=None):
        selected = self.table.selection()
        if selected:
            self.open(int(selected[0]))

    def open(self, tid):
        if tid not in self.rows:
            return
        if tid not in self.tabs:
            frame = ttk.Frame(self.book, padding=10)
            ttk.Button(frame, text='До списку чатів', command=self.show_list).pack(anchor='w')
            history = ScrolledText(frame, wrap='word', height=12, state='disabled')
            history.pack(fill='both', expand=True, pady=8)
            status = tk.StringVar()
            ttk.Label(frame, textvariable=status, wraplength=700).pack(fill='x')
            entry = ScrolledText(frame, wrap='word', height=4)
            entry.pack(fill='x', pady=8)
            entry.insert('1.0', self.rows[tid]['draft'])
            entry.edit_modified(False)
            entry.bind('<<Modified>>', lambda event, t=tid: self.save(t))
            send = ttk.Button(frame, text='Надіслати', command=lambda: self.send(tid))
            send.pack(side='left')
            retry = ttk.Button(frame, text='Повторити', command=lambda: self.retry(tid))
            retry.pack(side='left', padx=8)
            ttk.Button(frame, text='Завершити звернення', command=lambda: self.finish(tid)).pack(side='right')
            self.tabs[tid] = dict(frame=frame, history=history, entry=entry, status=status, send=send,
                                  retry=retry, signature=None, pending=False, reply_id=None)
            self.tabs[tid]['awaiting'] = False
            self.tabs[tid]['cleared_reply'] = None
            self.book.add(frame, text=f'№{tid}')
        self.book.select(self.tabs[tid]['frame'])
        self.tabs[tid]['entry'].focus_set()
        self.update_tab(tid)

    def save(self, tid):
        tab = self.tabs[tid]
        entry = tab['entry']
        if entry.edit_modified():
            text = entry.get('1.0', 'end-1c')
            entry.edit_modified(False)
            self.command(dict(action='draft', ticket_id=tid, text=text))

    def selected(self, event=None):
        self.active = next((tid for tid, tab in self.tabs.items()
                            if str(tab['frame']) == self.book.select()), None)
        if self.active is not None:
            self.mark_read(self.active)
            self.tabs[self.active]['entry'].focus_set()

    def show_list(self):
        self.book.select(self.list_frame)
        self.active = None

    def mark_read(self, tid):
        history = self.rows[tid]['history']
        self.command(dict(action='read', ticket_id=tid, read_id=history[-1]['id'] if history else 0))

    def send(self, tid):
        tab = self.tabs[tid]
        text = tab['entry'].get('1.0', 'end-1c')
        if tab['pending'] or not text.strip():
            return
        if len(text.encode('utf-16-le')) // 2 > 2000:
            tab['status'].set('Відповідь має містити від 1 до 2000 одиниць UTF-16. Текст збережено.')
            return
        tab['pending'] = True
        tab['awaiting'] = True
        tab['send'].configure(state='disabled')
        tab['status'].set('Очікування доставки…')
        self.command(dict(action='send', ticket_id=tid, text=text))

    def retry(self, tid):
        tab = self.tabs[tid]
        if tab.get('awaiting') or not tab['reply_id']:
            return
        if messagebox.askyesno('Повторити відправку', 'Переконайтеся, що клієнт не отримав відповідь. Повторити збережене повідомлення?', parent=self):
            tab['pending'] = True
            tab['awaiting'] = True
            tab['retry'].configure(state='disabled')
            self.command(dict(action='retry', ticket_id=tid, reply_id=tab['reply_id']))

    def finish(self, tid):
        if messagebox.askyesno('Завершення звернення', 'Завершити звернення та видалити його історію згідно з чинною політикою зберігання?', parent=self):
            self.command(dict(action='close', ticket_id=tid))

    def update_tab(self, tid):
        tab, row = self.tabs[tid], self.rows[tid]
        history = row['history']
        signature = [(m['id'], m['body']) for m in history]
        if signature != tab['signature']:
            view = tab['history']
            view.configure(state='normal')
            view.delete('1.0', 'end')
            view.insert('1.0', '\n\n'.join(('Клієнт' if m['direction']=='client' else 'Спеціаліст') + ': ' + m['body'] for m in history))
            view.configure(state='disabled')
            view.see('end')
            tab['signature'] = signature
        latest = row['replies'][-1] if row['replies'] else None
        state = latest['status'] if latest else ''
        tab['pending'] = tab['awaiting'] or state in ('pending', 'sending', 'unknown')
        tab['reply_id'] = latest['id'] if latest else None
        tab['send'].configure(state='disabled' if tab['pending'] else 'normal')
        tab['retry'].configure(state='normal' if state in ('failed', 'unknown') and not tab['awaiting'] else 'disabled')
        if state == 'sent':
            tab['status'].set(SUCCESS)
            if tab['cleared_reply'] != latest['id'] and tab['entry'].get('1.0', 'end-1c') == latest['body']:
                tab['entry'].delete('1.0', 'end')
            tab['cleared_reply'] = latest['id']
        elif latest:
            tab['status'].set(latest['error'] or 'Очікування доставки…')
        self.book.tab(tab['frame'], text=f"№{tid} · {row['unread']}")

    def snapshot(self, rows):
        self.rows = {r['id']: r for r in rows}
        for tid in list(self.tabs):
            if tid not in self.rows:
                self.tabs.pop(tid)['frame'].destroy()
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        self.label.set('Чати · ' + str(sum(r['unread'] for r in rows)))
        for row in rows:
            last = row['history'][-1]['body'] if row['history'] else ''
            self.table.insert('', 'end', iid=str(row['id']), values=(row['id'], row['client_full_name'] or row['client_name'],
                'У роботі' if row['accepted_by'] else 'Нове', ' '.join(last.split())[:80], row['unread']))
            if row['id'] in self.tabs:
                self.update_tab(row['id'])
        if (self.winfo_viewable() and self.winfo_toplevel().state() != 'iconic'
                and self.active in self.rows and self.rows[self.active]['unread']):
            self.mark_read(self.active)
