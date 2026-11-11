import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.engine import Engine
from infobot.settings import Settings
from infobot.store import Store


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'connections.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.engine = Engine(self.store, Settings(operator_id=900, allowed_user_ids=[100]),
                             [{'id': 'test', 'question': 'Question?', 'answer': 'Answer.'}])

    def update(self, number=1, uid=100, chat_type='private', callback=False, **actor):
        message = {'message_id': 42, 'chat': {'id': uid, 'type': chat_type}, 'text': '/start'}
        user = {'id': uid, **actor}
        if callback:
            return {'update_id': number, 'callback_query': {
                'id': str(number), 'data': 'notification_menu', 'from': user, 'message': message}}
        return {'update_id': number, 'message': {**message, 'from': user}}

    def test_first_connection_persists_and_profile_refresh_preserves_time(self):
        with patch('infobot.store.time.time', return_value=1000):
            self.engine.process(self.update(first_name='Олена', last_name='Шевченко', username='olena'))
        with patch('infobot.store.time.time', return_value=2000):
            self.engine.process(self.update(2, callback=True, first_name='Олена', username='new_name'))
        with closing(Store(self.path)) as reopened:
            self.assertEqual(reopened.connections(), [{
                'user_id': 100, 'name': 'Олена', 'username': 'new_name', 'connected_at': 1000.0}])

    def test_groups_bots_and_denied_users_are_not_connections(self):
        self.engine.process(self.update(1, chat_type='group'))
        self.engine.process(self.update(2, uid=200))
        self.engine.process(self.update(3, is_bot=True))
        self.assertEqual(self.store.connections(), [])
        self.engine.process(self.update(4))
        self.assertEqual(self.store.connections()[0]['name'], '')

    def test_duplicate_update_does_not_change_profile_or_time(self):
        update = self.update(first_name='First')
        self.engine.process(update)
        before = self.store.connections()
        update['message']['from']['first_name'] = 'Changed'
        self.engine.process(update)
        self.assertEqual(self.store.connections(), before)

