from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from infobot.engine import Engine, Pairing, load_faq, text_size
from infobot.runner import Delivery, Runner
from infobot.settings import InstanceLock, ROOT, Settings, load_token, save_token
from infobot.store import Store
from infobot.telegram import APIError, TelegramAPI


class FakeAPI:
    def __init__(self, error=None):
        self.calls = []
        self.message_id = 1000
        self.error = error

    def call(self, method, payload):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        self.message_id += 1
        return {"message_id": self.message_id, "date": int(time.time())}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.settings = Settings(operator_id=900)
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / "faq.json"))
        self.sequence = 0

    def tearDown(self):
        self.store.close()

    def message(self, uid, text=None, chat_type="private", **extra):
        self.sequence += 1
        message = {"message_id": self.sequence, "date": int(time.time()), "from": {"id": uid},
                   "chat": {"id": uid, "type": chat_type}, **extra}
        if text is not None:
            message["text"] = text
        update = {"update_id": self.sequence, "message": message}
        self.engine.process(update)
        return update

    def click(self, uid, data, chat_id=None, message_id=None):
        if message_id is None:
            # A user can click only after Telegram has delivered a keyboard.
            if self.store.menu_message(uid) is None:
                self.message(uid, "/start")
                self.drain()
            message_id = self.store.menu_message(uid)["message_id"]
        self.sequence += 1
        update = {"update_id": self.sequence, "callback_query": {"id": str(self.sequence),
                  "from": {"id": uid}, "data": data,
                  "message": {"message_id": message_id, "chat": {"id": uid if chat_id is None else chat_id, "type": "private"}}}}
        self.engine.process(update)
        return update

    def payloads(self, uid=None):
        rows = self.store.db.execute("SELECT * FROM outbox WHERE method='sendMessage' ORDER BY id").fetchall()
        return [json.loads(row["payload"]) for row in rows if uid is None or row["destination"] == uid]

    def create(self, uid=100):
        self.click(uid, "new")
        self.message(uid, "Вигадане запитання клієнта")
        self.finish_contacts(uid)
        return self.store.user_ticket(uid)["id"]

    def finish_contacts(self, uid=100):
        for text in (f'client{uid}@example.test', '+380671234567', 'Шевченко Олена Іванівна', 'ТОВ "Укрхелп"'):
            self.message(uid, text)

    def drain(self, api=None):
        api = api if api is not None else FakeAPI()
        delivery = Delivery(self.store, api, lambda *event: None)
        for _ in range(100):
            if not self.store.counts()[1]:
                break
            delivery.sent_at.clear()
            delivery.flush()
        self.assertEqual(self.store.counts()[1], 0)
        return api

    def test_click_passes_current_id_and_stale_buttons_are_filtered(self):
        self.message(100, "/start")
        self.drain()
        menu_id = self.store.menu_message(100)["message_id"]
        update = self.click(100, "new")
        self.assertEqual(update["callback_query"]["message"]["message_id"], menu_id)
        pending = self.engine.pending[100]
        before = self.store.counts()[1]
        self.click(100, "menu", message_id=menu_id - 1)
        self.assertEqual(self.engine.pending[100], pending)
        self.assertEqual(self.store.counts()[1], before + 1)
        self.click(100, "menu")
        self.assertNotIn(100, self.engine.pending)

    def test_start_and_faq_without_ticket(self):
        self.message(100, "/start")
        self.assertIn("Оберіть", self.payloads(100)[-1]["text"])
        self.click(100, "faq:about")
        self.assertIn("переробної промисловості", self.payloads(100)[-1]["text"])
        self.assertEqual(self.store.counts()[0], 0)

    def test_navigation_edits_one_message_and_hides_typed_commands(self):
        start = self.message(100, "/START@demo_infobot")
        api = self.drain()
        menu_id = self.store.menu_message(100)["message_id"]
        self.assertEqual([method for method, _ in api.calls], ["sendMessage", "deleteMessage"])
        self.assertEqual(api.calls[-1][1], {"chat_id": 100, "message_id": start["message"]["message_id"]})
        api.calls.clear()

        self.click(100, "faq:about")
        support = self.message(100, "/support")
        self.message(100, "Звичайний текст")
        self.message(100, "/start", chat_type="group")
        self.drain(api)
        edits = [payload for method, payload in api.calls if method == "editMessageText"]
        self.assertEqual([payload["message_id"] for payload in edits], [menu_id] * 3)
        self.assertIn("переробної промисловості", edits[0]["text"])
        self.assertIn("Підтримка", edits[1]["text"])
        self.assertNotEqual(edits[0]["reply_markup"], edits[1]["reply_markup"])
        self.assertFalse(any(method == "sendMessage" for method, _ in api.calls))
        self.assertEqual([p for method, p in api.calls if method == "deleteMessage"],
                         [{"chat_id": 100, "message_id": support["message"]["message_id"]}])

    def test_ticket_notifications_remain_separate_and_navigation_releases_menu_reference(self):
        self.message(100, "/start")
        self.message(900, "/start")
        api = self.drain()
        menu_ids = [self.store.menu_message(uid)["message_id"] for uid in (100, 900)]
        api.calls.clear()

        ticket_id = self.create()
        reply = self.message(900, f"/reply {ticket_id} Тестова відповідь")
        self.click(100, "support")
        self.click(900, "faq")
        self.drain(api)
        sent = [p for method, p in api.calls if method == "sendMessage"]
        self.assertEqual([p["chat_id"] for p in sent], [900, 100])
        self.assertIn("Вигадане запитання", sent[0]["text"])
        self.assertIn("Тестова відповідь", sent[1]["text"])
        self.assertIn(("deleteMessage", {"chat_id": 900, "message_id": reply["message"]["message_id"]}), api.calls)
        self.assertEqual([self.store.menu_message(uid)["message_id"] for uid in (100, 900)], menu_ids)

        api.calls.clear()
        self.click(900, f"close:{ticket_id}")
        self.drain(api)
        deleted = [mid for method, p in api.calls if method == "deleteMessages" for mid in p["message_ids"]]
        self.assertTrue(deleted)
        self.assertTrue(set(menu_ids).isdisjoint(deleted))
        self.assertTrue(any(method == "sendMessage" and p["text"].startswith("Олено,") for method, p in api.calls))
        closures = [p for method, p in api.calls if method == "sendMessage" and 'закрите.' in p["text"]]
        self.assertEqual(len(closures), 2)
        for payload in closures:
            self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "notification_menu")

    def test_ticket_closure_cleans_menus_still_showing_ticket(self):
        ticket_id = self.create()
        self.click(900, f"ticket:{ticket_id}")
        api = self.drain()
        client_menu_id = self.store.menu_message(100)["message_id"]
        operator_menu_id = self.store.menu_message(900)["message_id"]
        with self.store.db:
            self.store.record_menu_message(100, client_menu_id, time.time() - 49 * 3600, ticket_id)
        api.calls.clear()

        self.click(900, f"close:{ticket_id}")
        self.drain(api)
        edits = [p for method, p in api.calls if method == "editMessageText"]
        self.assertEqual([(p["chat_id"], p["message_id"]) for p in edits], [(100, client_menu_id)])
        self.assertNotIn("client100@example.test", edits[0]["text"])
        self.assertTrue(any(method == "deleteMessages" and operator_menu_id in p["message_ids"] for method, p in api.calls))
        self.assertIsNone(self.store.menu_message(900))

    def test_ticket_reply_and_close_purges_content(self):
        ticket_id = self.create()
        self.message(900, f"/reply {ticket_id} Тестова відповідь")
        self.assertEqual(len(self.store.history(ticket_id)), 2)
        self.assertTrue(any("Тестова відповідь" in p["text"] for p in self.payloads(100)))
        self.drain()
        self.assertGreater(self.store.db.execute("SELECT count(*) FROM telegram_refs").fetchone()[0], 1)
        self.click(900, f"close:{ticket_id}")
        self.assertEqual(self.store.counts()[0], 0)
        for table in ("messages", "telegram_refs"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        queued = str([p for p in self.payloads()])
        self.assertNotIn("Вигадане запитання", queued)
        self.assertNotIn("Тестова відповідь", queued)
        api = self.drain()
        self.assertTrue(any(method == "deleteMessages" for method, payload in api.calls))

    def test_unrelated_user_cannot_read_reply_or_close(self):
        ticket_id = self.create()
        for action in ("ticket", "reply", "close", "confirm"):
            self.click(200, f"{action}:{ticket_id}")
        self.message(200, f"/reply {ticket_id} malicious")
        self.assertIsNotNone(self.store.ticket(ticket_id))
        self.assertEqual(len(self.store.history(ticket_id)), 1)
        self.assertTrue(all("Вигадане запитання" not in p["text"] for p in self.payloads(200)))

    def test_ticket_owner_cannot_impersonate_operator(self):
        ticket_id = self.create()
        self.click(100, f"reply:{ticket_id}")
        self.assertNotIn(100, self.engine.pending)
        self.message(100, f"/reply {ticket_id} чужа відповідь")
        self.assertEqual(len(self.store.history(ticket_id)), 1)

    def test_only_one_open_ticket_per_user(self):
        ticket_id = self.create()
        self.click(100, "new")
        self.message(100, "Додаткова інформація")
        self.assertEqual(self.store.counts()[0], 1)
        self.assertEqual(len(self.store.history(ticket_id)), 2)

    def test_close_cancels_undelivered_ticket_messages(self):
        ticket_id = self.create()
        self.click(100, f"close:{ticket_id}")
        self.assertTrue(all("Вигадане запитання" not in p["text"] for p in self.payloads()))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE ticket_id IS NOT NULL").fetchone()[0], 0)

    def test_closed_ticket_buttons_cannot_reopen(self):
        ticket_id = self.create()
        self.click(100, f"close:{ticket_id}")
        self.click(900, f"reply:{ticket_id}")
        self.message(900, "Пізня відповідь")
        self.assertEqual(self.store.counts()[0], 0)

    def test_closing_one_ticket_does_not_affect_other(self):
        first = self.create(100)
        second = self.create(200)
        self.click(900, f"close:{first}")
        self.assertIsNotNone(self.store.ticket(second))
        self.assertEqual(len(self.store.history(second)), 1)

    def test_duplicate_update_does_not_duplicate_state(self):
        self.click(100, "new")
        update = self.message(100, "Одне запитання")
        self.finish_contacts()
        before = self.store.counts()
        self.engine.process(update)
        self.assertEqual(self.store.counts(), before)
        self.assertEqual(len(self.store.history(1)), 1)

    def test_transaction_rolls_back_offset_and_messages(self):
        self.click(100, "new")
        offset = self.store.offset
        with patch.object(self.store, "create_draft", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.message(100, "Не має загубитися")
        self.assertEqual(self.store.offset, offset)
        self.assertEqual(self.store.counts()[0], 0)
        self.assertEqual(self.engine.pending[100][0], "new")

    def test_group_and_forged_callback_are_ignored(self):
        self.message(100, "/start", chat_type="group")
        self.click(100, "new", chat_id=200, message_id=123)
        self.assertEqual(self.store.counts(), (0, 0))

    def test_first_user_never_becomes_operator(self):
        self.settings.operator_id = 0
        self.message(111, "/start")
        self.assertEqual(self.settings.operator_id, 0)
        self.click(111, "new")
        self.assertNotIn(111, self.engine.pending)

    def test_pairing_requires_code_and_is_single_use(self):
        self.settings.operator_id = 0
        self.message(100, "/claim invalid")
        self.assertEqual(self.settings.operator_id, 0)
        code = self.engine.pairing.issue()
        self.message(900, "/claim " + code)
        self.assertEqual(self.settings.operator_id, 900)
        self.message(100, "/claim " + code)
        self.assertEqual(self.settings.operator_id, 900)

    def test_unicode_pairing_input_does_not_crash(self):
        self.engine.pairing.issue()
        self.message(100, "/claim неправильний-код")
        self.assertEqual(self.settings.operator_id, 900)
        self.assertIn("недійсний", self.payloads(100)[-1]["text"])

    def test_pairing_allows_operator_not_on_test_allowlist(self):
        self.settings.allowed_user_ids = [100]
        code = self.engine.pairing.issue()
        self.message(777, "/claim " + code)
        self.assertEqual(self.settings.operator_id, 777)

    def test_operator_replacement_revokes_access_and_pending_notifications(self):
        ticket_id = self.create()
        code = self.engine.pairing.issue()
        self.message(777, "/claim " + code)
        self.assertEqual(self.settings.operator_id, 777)
        self.assertEqual(self.payloads(900), [])
        self.click(900, f"ticket:{ticket_id}")
        self.assertIn("недоступне", self.payloads(900)[-1]["text"])
        self.click(777, f"ticket:{ticket_id}")
        self.assertIn("Вигадане запитання", self.payloads(777)[-1]["text"])

    def test_allowlist_blocks_other_clients(self):
        self.settings.allowed_user_ids = [100]
        self.click(200, "new")
        self.message(200, "Не зберігати")
        self.assertEqual(self.store.counts()[0], 0)
        self.create(100)
        self.assertEqual(self.store.counts()[0], 1)

    def test_no_media_is_downloaded_or_saved(self):
        self.click(100, "new")
        self.message(100, photo=[{"file_id": "test"}])
        self.assertEqual(self.store.counts()[0], 0)
        self.assertIn("текстові", self.payloads(100)[-1]["text"])

    def test_utf16_limits_with_emoji(self):
        self.click(100, "new")
        self.message(100, "😀" * 1100)
        self.assertEqual(self.store.counts()[0], 0)
        self.message(100, "😀" * 1000)
        self.finish_contacts()
        self.assertEqual(self.store.counts()[0], 1)
        self.assertTrue(all(text_size(p["text"]) <= 4096 for p in self.payloads()))

    def test_delete_me_and_cancel(self):
        self.click(100, "new")
        self.message(100, "/cancel")
        self.message(100, "Не звернення")
        self.assertEqual(self.store.counts()[0], 0)
        self.create()
        self.message(100, "/delete_me")
        self.assertEqual(self.store.counts()[0], 0)

    def test_reply_buttons_and_confirmation(self):
        ticket_id = self.create()
        self.click(900, f"reply:{ticket_id}")
        self.message(900, "Відповідь через кнопку")
        self.click(100, f"confirm:{ticket_id}")
        self.assertIsNotNone(self.store.ticket(ticket_id))
        self.assertTrue(any("Так, закрити" in str(p.get("reply_markup")) for p in self.payloads(100)))

    def test_user_can_continue_ticket_after_engine_restart(self):
        ticket_id = self.create()
        self.engine = Engine(self.store, self.settings, load_faq(ROOT / "faq.json"))
        self.message(100, "Після перезапуску")
        self.assertEqual(len(self.store.history(ticket_id)), 2)

    def test_deletion_can_be_disabled_and_old_messages_are_skipped(self):
        ticket_id = self.create()
        self.store.track(ticket_id, 100, 500, time.time() - 49 * 3600)
        self.store.track(ticket_id, 100, 501, time.time())
        self.click(100, f"close:{ticket_id}")
        deleted = [json.loads(row[0])["message_ids"] for row in self.store.db.execute("SELECT payload FROM outbox WHERE method='deleteMessages'")]
        self.assertNotIn(500, [mid for batch in deleted for mid in batch])
        self.assertIn(501, [mid for batch in deleted for mid in batch])
        self.settings.delete_telegram_on_close = False
        ticket_id = self.create(200)
        before = self.store.db.execute("SELECT count(*) FROM outbox WHERE method='deleteMessages'").fetchone()[0]
        self.click(200, f"close:{ticket_id}")
        after = self.store.db.execute("SELECT count(*) FROM outbox WHERE method='deleteMessages'").fetchone()[0]
        self.assertEqual(before, after)

    def test_admin_pagination(self):
        for uid in range(100, 110):
            self.create(uid)
        self.click(900, "tickets:1")
        markup = self.payloads(900)[-1]["reply_markup"]
        self.assertEqual(sum(1 for row in markup["inline_keyboard"] for button in row if button["callback_data"].startswith("ticket:")), 2)


class InfrastructureTests(unittest.TestCase):
    def test_menu_survives_database_restart_and_temporary_edit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            store = Store(path)
            created_at = time.time() - 3600
            with store.db:
                store.record_menu_message(100, 42, created_at, None)
                store.enqueue("sendMessage", {"chat_id": 100, "text": "Підтримка"}, is_menu=True)
            store.close()

            with closing(Store(path)) as store:
                api = FakeAPI(APIError(429, 7))
                Delivery(store, api, lambda *args: None).flush()
                self.assertEqual(api.calls, [("editMessageText", {"chat_id": 100, "text": "Підтримка", "message_id": 42})])
                self.assertEqual(store.counts()[1], 1)
                self.assertEqual(store.menu_message(100)["created_at"], created_at)

                with store.db:
                    store.db.execute("UPDATE outbox SET next_attempt=0")
                api.error = None
                Delivery(store, api, lambda *args: None).flush()
                self.assertEqual(api.calls[0], api.calls[-1])
                self.assertEqual(store.menu_message(100)["message_id"], 42)
                self.assertEqual(store.counts()[1], 0)

    def test_menu_unchanged_is_success_and_missing_menu_is_replaced(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        payload = {"chat_id": 100, "text": "Головне меню"}
        with store.db:
            store.record_menu_message(100, 42, time.time(), None)
            store.enqueue("sendMessage", payload, is_menu=True)
        api = FakeAPI()
        with patch.object(api, "call", side_effect=APIError(400, description="Bad Request: message is not modified")) as call:
            Delivery(store, api, lambda *args: None).flush()
            call.assert_called_once_with("editMessageText", {**payload, "message_id": 42})
        self.assertEqual(store.counts()[1], 0)
        self.assertEqual(store.menu_message(100)["message_id"], 42)

        with store.db:
            store.enqueue("sendMessage", payload, is_menu=True)
        with patch.object(api, "call", side_effect=[APIError(400, description="Bad Request: message to edit not found"),
                                                   {"message_id": 43, "date": int(time.time())},
                                                   APIError(400, description="Bad Request: message to edit not found")]) as call:
            Delivery(store, api, lambda *args: None).flush()
            self.assertEqual([entry.args for entry in call.call_args_list],
                             [("editMessageText", {**payload, "message_id": 42}), ("sendMessage", payload),
                              ("editMessageReplyMarkup", {"chat_id": 100, "message_id": 42,
                                                          "reply_markup": {"inline_keyboard": []}})])
        self.assertEqual(store.menu_message(100)["message_id"], 43)
        self.assertEqual(store.counts()[1], 0)

    def test_expired_and_exhausted_pairing(self):
        pairing = Pairing()
        code = pairing.issue()
        pairing.expires = 0
        self.assertFalse(pairing.redeem(code))
        code = pairing.issue()
        for _ in range(20):
            self.assertFalse(pairing.redeem("wrong"))
        self.assertFalse(pairing.redeem(code))

    def test_backups_are_consistent_and_offset_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            store = Store(path)
            with store.db:
                ticket_id = store.create_ticket(100)
                store.add_message(ticket_id, "client", "Запитання")
                store.set_offset(55)
                store.enqueue("sendMessage", {"chat_id": 900, "text": "Запитання"}, ticket_id)
            backup = store.backup(Path(directory) / "backups")
            store.close()
            restored = Store(backup)
            self.assertEqual(restored.offset, 55)
            self.assertEqual(restored.counts(), (1, 1))
            self.assertEqual(restored.history(ticket_id)[0]["body"], "Запитання")
            restored.close()

    def test_queue_retries_network_and_preserves_chat_order(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        with store.db:
            store.enqueue("sendMessage", {"chat_id": 1, "text": "first"})
            store.enqueue("sendMessage", {"chat_id": 1, "text": "second"})
            store.enqueue("sendMessage", {"chat_id": 2, "text": "independent"})
        api = FakeAPI(APIError(429, 7))
        delivery = Delivery(store, api, lambda *args: None)
        delivery.flush()
        self.assertEqual(store.counts()[1], 3)
        self.assertEqual(len(api.calls), 2)
        self.assertGreater(store.db.execute("SELECT next_attempt FROM outbox ORDER BY id LIMIT 1").fetchone()[0], time.time() + 5)
        self.assertEqual(len(store.ready_jobs(time.time())), 0)

    def test_permanent_delivery_error_drops_only_failed_job(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        with store.db:
            store.enqueue("sendMessage", {"chat_id": 1, "text": "first"})
            store.enqueue("sendMessage", {"chat_id": 1, "text": "second"})
        Delivery(store, FakeAPI(APIError(403)), lambda *args: None).flush()
        self.assertEqual(store.counts()[1], 1)

    def test_delivery_authentication_error_stops_without_losing_queue(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        with store.db:
            store.enqueue("sendMessage", {"chat_id": 1, "text": "first"})
        with self.assertRaises(APIError):
            Delivery(store, FakeAPI(APIError(401)), lambda *args: None).flush()
        self.assertEqual(store.counts()[1], 1)

    def test_token_roundtrip_and_settings_do_not_contain_token(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"INFOBOT_TOKEN": ""}):
            token = "123456789:" + "A" * 35
            secret = Path(directory) / "token.secret"
            save_token(secret, token)
            self.assertEqual(load_token(secret), token)
            path = Path(directory) / "config.json"
            Settings(operator_id=900, allowed_user_ids=[100]).save(path)
            self.assertNotIn(token, path.read_text())
            self.assertEqual(Settings.load(path).operator_id, 900)
            if os.name != "nt":
                self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            else:
                self.assertNotIn(token.encode(), secret.read_bytes())

    def test_invalid_config_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for content in ('[]', '{"operator_id":true}', '{"allowed_user_ids":[-1]}', '{"delete_telegram_on_close":"true"}'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    Settings.load(path)

    def test_token_does_not_leak_through_network_errors(self):
        from urllib.error import URLError
        token = "123456789:" + "A" * 35
        with patch("infobot.telegram.urlopen", side_effect=URLError("https://api.telegram.org/bot" + token)):
            with self.assertRaises(APIError) as result:
                TelegramAPI(token).call("getMe")
        self.assertNotIn(token, str(result.exception))

    def test_http_edit_error_classifies_missing_menu_without_exposing_details(self):
        from io import BytesIO
        from urllib.error import HTTPError
        detail = "Bad Request: message to edit not found"
        error = HTTPError("https://api.telegram.org/", 400, "Bad Request", None,
                          BytesIO(json.dumps({"description": detail}).encode()))
        with patch("infobot.telegram.urlopen", side_effect=error), self.assertRaises(APIError) as result:
            TelegramAPI("test").call("editMessageText")
        self.assertTrue(result.exception.edit_unavailable)
        self.assertNotIn(detail, str(result.exception))

    def test_faq_validation_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "faq.json"
            path.write_text(json.dumps([{"id": "same", "question": "q", "answer": "a"}] * 2))
            with self.assertRaises(ValueError):
                load_faq(path)

    def test_runner_stops_on_auth_error(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "faq.json").write_bytes((ROOT / "faq.json").read_bytes())
            runner = Runner("123456789:" + "A" * 35, Settings(), lambda *event: events.append(event), root)
            with patch("infobot.runner.TelegramAPI", return_value=FakeAPI(APIError(401))):
                runner.start()
                runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive())
            self.assertTrue(any(kind == "error" for kind, value in events))
            self.assertEqual(events[-1][0], "stopped")

    def test_runner_polls_persists_and_delivers_with_fake_telegram(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "faq.json").write_bytes((ROOT / "faq.json").read_bytes())
            old_store = Store(root / "data" / "bot_123.sqlite3")
            with old_store.db:
                old_store.enqueue("setMyCommands", {"commands": [{"command": "commands", "description": "Усі команди"}]})
                old_menu = old_store.ready_jobs(time.time())[0]
                old_store.retry(old_menu["id"], 3600)
            old_store.close()
            runner = Runner("123456789:" + "A" * 35, Settings(operator_id=900),
                            lambda *event: events.append(event), root)
            class Server:
                def __init__(self):
                    self.rounds = 0
                    self.calls = []
                def call(self, method, payload=None):
                    self.calls.append((method, payload))
                    if method == "getMe":
                        return {"id": 123, "username": "offline_test_bot"}
                    if method == "getWebhookInfo":
                        return {"url": ""}
                    if method == "getUpdates":
                        self.rounds += 1
                        if self.rounds > 1:
                            runner.stop.set()
                            return []
                        return [{"update_id": index, "message": {
                            "message_id": index, "date": int(time.time()), "from": {"id": 100},
                            "chat": {"id": 100, "type": "private"}, "text": text
                        }} for index, text in enumerate(('/ask', 'Запитання через опитування', 'client@example.test',
                                                        '+380671234567', 'Шевченко Олена Іванівна', 'ТОВ "Укрхелп"'), 1)]
                    return {"message_id": len(self.calls), "date": int(time.time())}
            server = Server()
            with patch("infobot.runner.TelegramAPI", return_value=server):
                runner.start()
                runner.thread.join(3)
            self.assertFalse(runner.thread.is_alive())
            self.assertFalse(any(kind == "error" for kind, value in events))
            store = Store(root / "data" / "bot_123.sqlite3")
            self.assertEqual(store.offset, 7)
            self.assertEqual(store.counts()[0], 1)
            self.assertEqual(store.history(1)[0]["body"], "Запитання через опитування")
            store.close()
            self.assertTrue(any(method == "sendMessage" and payload["chat_id"] == 900 for method, payload in server.calls))
            menus = [payload for method, payload in server.calls if method == "setMyCommands"]
            self.assertEqual(len(menus), 1)
            self.assertEqual({item["command"] for item in menus[0]["commands"]},
                             {"start", "faq", "support", "ask", "mine", "continue", "privacy", "cancel", "back"})

    def test_os_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with InstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(path):
                        pass
            with InstanceLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
