from __future__ import annotations

import time
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from .engine import Engine, load_faq
from .runner import Delivery
from .settings import ROOT, Settings, MailSettings
from .mailer import MailDelivery, MailTemplate
from .store import Store
from .telegram import APIError


class Demo(ttk.Frame):
    """Two local simulated Telegram clients using the actual business logic."""

    def __init__(self, master, faq_source=None):
        super().__init__(master, padding=14)
        self.store = None
        self.faq_source = faq_source or (lambda: load_faq(ROOT / 'faq.json'))
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Діалог і листи без інтернету та токена.", font=("Segoe UI", 11)).pack(side="left")
        ttk.Button(header, text="Почати знову", command=self.reset).pack(side="right")
        ttk.Button(header, text="Переглянути лист", command=self.preview_mail).pack(side="right", padx=8)
        ttk.Label(self, text='Після запитання введіть email, телефон, ПІБ (Шевченко Олена Іванівна) та компанію ТОВ "Укрхелп". До завершення всіх кроків лист не формується.', wraplength=860).pack(fill='x', pady=(0, 8))
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.reset()

    def reset(self):
        if self.store:
            self.store.close()
        for child in self.body.winfo_children():
            child.destroy()
        self.store = Store(":memory:")
        settings = Settings(operator_id=2, mail=MailSettings(enabled=True, host="smtp.example.test",
                            username="bot@example.test", sender="bot@example.test", recipient="support@example.test"))
        self.engine = Engine(self.store, settings, self.faq_source(),
                             mail_template=MailTemplate.load(ROOT / "mail_template.json"), bot_username="demo_infobot")
        self.demo_letters = []
        class LocalMailbox:
            def __init__(self, letters):
                self.letters = letters
            def send(self, payload):
                self.letters.append(payload.copy())
        self.mail_delivery = MailDelivery(self.store, LocalMailbox(self.demo_letters), lambda *args: None)
        self.telegram_delivery = Delivery(self.store, self, lambda *args: None)
        self.update_id = 0
        self.message_ids = {1: 0, 2: 0}
        self.views = {}
        self.inputs = {}
        self.buttons = {}
        for column, (user_id, title) in enumerate(((1, "Клієнт · демо"), (2, "Диспетчер · демо"))):
            self.body.columnconfigure(column, weight=1, uniform="chat")
            self.body.rowconfigure(0, weight=1)
            panel = ttk.LabelFrame(self.body, text=title, padding=10)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
            view = ScrolledText(panel, wrap="word", width=35, height=12, font=("Segoe UI", 10),
                                bg="#ffffff", relief="flat", padx=8, pady=8, state="disabled")
            view.pack(fill="both", expand=True)
            self.views[user_id] = view
            buttons = ttk.Frame(panel)
            buttons.pack(fill="x", pady=8)
            self.buttons[user_id] = buttons
            entry = ttk.Entry(panel)
            entry.pack(fill="x", pady=(0, 6))
            entry.bind("<Return>", lambda event, uid=user_id: self.send(uid))
            self.inputs[user_id] = entry
            ttk.Button(panel, text="Надіслати", command=lambda uid=user_id: self.send(uid)).pack(fill="x")
        for user_id in (1, 2):
            self.send(user_id, "/start")

    def append(self, user_id: int, text: str, message_id: int):
        view = self.views[user_id]
        view.configure(state="normal")
        view.insert("end", text + "\n\n", f"message_{message_id}")
        view.configure(state="disabled")
        view.see("end")

    def send(self, user_id: int, text: str | None = None):
        text = self.inputs[user_id].get() if text is None else text
        if not text.strip():
            return
        self.inputs[user_id].delete(0, "end")
        self.message_ids[user_id] += 1
        self.update_id += 1
        message_id = self.message_ids[user_id]
        self.append(user_id, "Ви: " + text, message_id)
        self.engine.process({"update_id": self.update_id, "message": {
            "message_id": message_id, "date": int(time.time()),
            "from": {"id": user_id, "first_name": "Тестовий клієнт" if user_id == 1 else "Диспетчер", "username": "demo_client" if user_id == 1 else "demo_operator"},
            "chat": {"id": user_id, "type": "private"}, "text": text}})
        self.drain()

    def click(self, user_id: int, data: str):
        self.update_id += 1
        self.engine.process({"update_id": self.update_id, "callback_query": {
            "id": str(self.update_id), "from": {"id": user_id}, "data": data,
            "message": {"message_id": self.message_ids[user_id], "chat": {"id": user_id, "type": "private"}}}})
        self.drain()

    def drain(self):
        while True:
            self.mail_delivery.flush_one()
            if not self.store.ready_jobs(time.time()):
                break
            self.telegram_delivery.sent_at.clear()
            self.telegram_delivery.flush()

    def call(self, method: str, payload: dict):
        if method == "answerCallbackQuery":
            return True
        user_id = payload["chat_id"]
        view = self.views[user_id]
        if method in ("deleteMessage", "deleteMessages"):
            ids = [payload["message_id"]] if method == "deleteMessage" else payload["message_ids"]
            view.configure(state="normal")
            for message_id in ids:
                ranges = view.tag_ranges(f"message_{message_id}")
                for start in range(len(ranges) - 2, -1, -2):
                    view.delete(ranges[start], ranges[start + 1])
            view.configure(state="disabled")
            return True
        text = "Бот: " + payload["text"]
        if method == "editMessageText":
            message_id = payload["message_id"]
            tag = f"message_{message_id}"
            ranges = view.tag_ranges(tag)
            if not ranges:
                raise APIError(400, description="message to edit not found")
            view.configure(state="normal")
            view.replace(ranges[0], ranges[-1], text + "\n\n", tag)
            view.configure(state="disabled")
            view.see(ranges[0])
        else:
            self.message_ids[user_id] += 1
            message_id = self.message_ids[user_id]
            self.append(user_id, text, message_id)
        if "reply_markup" in payload:
            self.render_buttons(user_id, payload["reply_markup"])
        return {"message_id": message_id, "date": int(time.time())}

    def preview_mail(self):
        if not self.demo_letters:
            messagebox.showinfo("Листи в деморежимі", "Спочатку створіть звернення в лівій панелі. Потім тут можна переглянути сформований лист.")
            return
        payload = self.demo_letters[-1]
        show_letter(self, payload, "Лист у деморежимі — насправді не надсилається")

    def render_buttons(self, user_id: int, markup: dict):
        frame = self.buttons[user_id]
        for child in frame.winfo_children():
            child.destroy()
        for row in markup.get("inline_keyboard", []):
            row_frame = ttk.Frame(frame)
            row_frame.pack(fill="x", pady=2)
            for button in row:
                if 'url' in button:
                    command = lambda url=button['url']: webbrowser.open(url)
                else:
                    command = lambda data=button['callback_data'], uid=user_id: self.click(uid, data)
                ttk.Button(row_frame, text=button["text"],
                           command=command
                           ).pack(side="left", fill="x", expand=True, padx=2)

    def destroy(self):
        if self.store:
            self.store.close()
            self.store = None
        super().destroy()


def show_letter(master, payload: dict, title: str = "Попередній перегляд листа"):
    window = tk.Toplevel(master)
    window.title(title)
    window.geometry("760x620")
    view = ScrolledText(window, wrap="word", font=("Segoe UI", 11), padx=14, pady=14)
    view.pack(fill="both", expand=True)
    view.insert("1.0", "Тема: " + payload["subject"] + "\n\n" + payload["body"])
    view.configure(state="disabled")
