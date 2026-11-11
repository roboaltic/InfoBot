import copy
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from infobot.engine import Engine
from infobot.runner import Delivery
from infobot.settings import Settings
from infobot.store import Store
from infobot.telegram import APIError


class MenuClient:
    """Model Telegram message keyboards without any network access."""

    def __init__(self, store, payload, edit_error=None, cleanup_error=None):
        self.store = store
        self.messages = {42: copy.deepcopy(payload)}
        self.calls = []
        self.edit_error = edit_error
        self.cleanup_error = cleanup_error
        self.send_error = None

    def call(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == "answerCallbackQuery":
            return True
        if method == "editMessageText":
            if self.edit_error:
                raise APIError(400, description=self.edit_error)
            self.messages[payload["message_id"]] = copy.deepcopy(payload)
            return {"message_id": payload["message_id"]}
        if method == "sendMessage":
            if self.send_error:
                raise self.send_error
            self.messages[43] = copy.deepcopy(payload)
            return {"message_id": 43, "date": int(time.time())}
        if method == "editMessageReplyMarkup":
            # Cleanup must happen after the new ID is committed.
            with sqlite3.connect(self.store_path) as db:
                assert db.execute("SELECT message_id FROM menu_messages WHERE chat_id=100").fetchone()[0] == 43
            if self.cleanup_error:
                raise APIError(400, description="Bad Request: message can't be edited")
            self.messages[payload["message_id"]]["reply_markup"] = copy.deepcopy(payload["reply_markup"])
            return True
        raise AssertionError(method)


class MenuReplacementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "menu.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.payload = {"chat_id": 100, "text": "Menu", "reply_markup": {
            "inline_keyboard": [[{"text": "Support", "callback_data": "support"}]]}}
        with self.store.db:
            self.store.record_menu_message(100, 42, time.time(), None)
            self.store.enqueue("sendMessage", self.payload, is_menu=True)

    def deliver(self, edit_error=None, cleanup_error=None):
        api = MenuClient(self.store, self.payload, edit_error, cleanup_error)
        api.store_path = self.path
        delivery = Delivery(self.store, api, lambda *args: None)
        delivery.flush()
        self.assertEqual(self.store.counts()[1], 0)

        return api, delivery

    def test_failed_replacement_preserves_id_and_retry_cleans_old_buttons(self):
        api = MenuClient(self.store, self.payload, "Bad Request: message can't be edited")
        api.store_path = self.path
        api.send_error = APIError(500)
        delivery = Delivery(self.store, api, lambda *args: None)
        delivery.flush()
        menu = self.store.menu_message(100)
        self.assertIsNotNone(menu)
        self.assertEqual(menu["message_id"], 42)
        self.assertEqual(self.store.counts()[1], 1)
        api.send_error = None
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET next_attempt=0")
        delivery.flush()
        self.assertEqual(self.store.menu_message(100)["message_id"], 43)
        self.assertEqual(api.calls[-1][0], "editMessageReplyMarkup")
        self.assertEqual(api.messages[42]["reply_markup"]["inline_keyboard"], [])

    def test_navigation_edits_same_message(self):
        api, _ = self.deliver()
        self.assertEqual([method for method, _ in api.calls], ["editMessageText"])
        self.assertEqual(self.store.menu_message(100)["message_id"], 42)

    def test_unchanged_does_not_send_new_menu(self):
        api, _ = self.deliver("Bad Request: message is not modified")
        self.assertEqual([method for method, _ in api.calls], ["editMessageText"])
        self.assertEqual(self.store.menu_message(100)["message_id"], 42)

    def test_replacement_clears_old_buttons(self):
        api, _ = self.deliver("Bad Request: message can't be edited")
        self.assertEqual(self.store.menu_message(100)["message_id"], 43)
        self.assertEqual(api.messages[43], self.payload)
        self.assertEqual(api.messages[42]["reply_markup"]["inline_keyboard"], [])

    def test_cleanup_rejected_new_menu_remains_usable(self):
        api, delivery = self.deliver("Bad Request: message can't be edited", cleanup_error=True)
        self.assertEqual([method for method, _ in api.calls],
                         ["editMessageText", "sendMessage", "editMessageReplyMarkup"])
        self.assertEqual(self.store.menu_message(100)["message_id"], 43)
        self.assertEqual(api.messages[43], self.payload)
        self.assertTrue(api.messages[42]["reply_markup"]["inline_keyboard"])
        api.edit_error = None
        with self.store.db:
            self.store.enqueue("sendMessage", {**self.payload, "text": "Next menu"}, is_menu=True)
        delivery.sent_at.clear()
        delivery.flush()
        self.assertEqual(api.calls[-1][1]["message_id"], 43)
        self.assertEqual(api.messages[43]["text"], "Next menu")
        self.assertEqual(self.store.counts()[1], 0)


class MenuCallbackTests(unittest.TestCase):
    def test_closure_notification_menu_new_and_legacy(self):
        texts = (
            'Олено, дякуємо за звернення! Ваше звернення №1 закрите. Листування видалено з робочої бази. Уже надіслані електронні листи залишаються у служби підтримки.',
            'Звернення №1 закрите. Листування видалено з робочої бази.',
        )
        for data, text in [("notification_menu", texts[0]), *(('menu', text) for text in texts)]:
            with self.subTest(data=data, text=text), closing(Store(":memory:")) as store:
                with store.db:
                    store.record_menu_message(100, 43, time.time(), None)
                engine = Engine(store, Settings(), [{"id": "about", "question": "Question?", "answer": "Answer"}])
                engine.process({"update_id": 1, "callback_query": {
                    "id": "closure", "from": {"id": 100}, "data": data,
                    "message": {"message_id": 44, "text": text,
                                "reply_markup": {"inline_keyboard": [[{"text": "Головне меню", "callback_data": data}]]},
                                "chat": {"id": 100, "type": "private"}}}})
                self.assertEqual(store.db.execute("SELECT count(*) FROM outbox WHERE is_menu=1").fetchone()[0], 1)
                import json
                self.assertEqual(json.loads(store.db.execute("SELECT payload FROM outbox WHERE is_menu=1").fetchone()[0])["text"], engine.content.data["greeting"])

    def test_all_static_actions_reject_stale_or_unrecorded_menu_before_state_changes(self):
        actions = ('menu', 'faq', 'faq:about', 'faqpage:0', 'support', 'help', 'privacy', 'identity',
                   'new', 'continue', 'back', 'cancel_draft', 'mine', 'admin', 'commands', 'tickets:0',
                   'faqmanage', 'faqedit:about', 'faqadd', 'faqfield:about:answer',
                   'faqremove:about', 'faqdelete:about:0')
        for saved in (False, True):
            for data in actions:
                with self.subTest(saved=saved, data=data), closing(Store(":memory:")) as store:
                    with store.db:
                        if saved:
                            store.record_menu_message(900, 43, time.time(), None)
                        store.create_draft(900, 'Question', '', '', 10, time.time())
                    engine = Engine(store, Settings(operator_id=900), [{"id": "about", "question": "Question?", "answer": "Answer"}])
                    engine.pending[900] = ('new', 0, 0)
                    engine.faq_edits[900] = {'sentinel': True}
                    before = dict(store.draft(900))
                    engine.process({"update_id": 1, "callback_query": {
                        "id": "stale", "from": {"id": 900}, "data": data,
                        "message": {"message_id": 42, "chat": {"id": 900, "type": "private"}}}})
                    self.assertEqual(engine.pending.get(900), ('new', 0, 0))
                    self.assertEqual(engine.faq_edits.get(900), {'sentinel': True})
                    self.assertEqual(dict(store.draft(900)), before)
                    self.assertEqual(store.counts()[1], 1)  # Callback acknowledgement only.

    def test_faq_from_old_and_current_menu(self):
        for message_id in (42, 43):
            with self.subTest(message_id=message_id), tempfile.TemporaryDirectory() as directory:
                with closing(Store(Path(directory) / "menu.sqlite3")) as store:
                    payload = {"chat_id": 100, "text": "Current menu", "reply_markup": {
                        "inline_keyboard": [[{"text": "Поширені запитання", "callback_data": "faq"}]]}}
                    with store.db:
                        store.record_menu_message(100, 43, time.time(), None)
                    api = MenuClient(store, payload)
                    api.messages[43] = copy.deepcopy(payload)
                    engine = Engine(store, Settings(), [{"id": "test", "question": "Test question?", "answer": "Test answer."}])
                    engine.process({"update_id": 1, "callback_query": {
                        "id": "faq-click", "from": {"id": 100}, "data": "faq",
                        "message": {"message_id": message_id, "chat": {"id": 100, "type": "private"}}}})
                    delivery = Delivery(store, api, lambda *args: None)
                    while store.counts()[1]:
                        delivery.sent_at.clear()
                        delivery.flush()
                    self.assertEqual(api.calls[0][0], "answerCallbackQuery")
                    self.assertEqual(api.calls[0][1]["callback_query_id"], "faq-click")
                    if message_id == 42:
                        self.assertIn("/start", api.calls[0][1]["text"])
                    self.assertEqual(store.menu_message(100)["message_id"], 43)
                    if message_id == 42:
                        self.assertEqual(len(api.calls), 1)
                        self.assertEqual(api.messages[43], payload)
                    else:
                        self.assertEqual([method for method, _ in api.calls],
                                         ["answerCallbackQuery", "editMessageText"])
                        self.assertEqual(api.calls[-1][1]["message_id"], 43)
                        self.assertIn("Поширені запитання", api.messages[43]["text"])
