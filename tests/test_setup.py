import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infobot.settings import Settings, load_token
from infobot.setup import configure


class SetupTests(unittest.TestCase):
    def test_setup_saves_settings_and_uses_entered_token(self):
        token = '123456789:' + 'A' * 35
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('infobot.setup.ROOT', root), patch('infobot.setup.getpass', return_value=token), \
                    patch('builtins.input', side_effect=['123', '456,789', 'y']), \
                    patch('builtins.print'), patch.dict('os.environ', {'INFOBOT_TOKEN': ''}):
                self.assertTrue(configure())
                self.assertEqual(load_token(root / 'data' / 'token.secret'), token)
                settings = Settings.load(root / 'data' / 'config.json')
                self.assertEqual(settings.operator_id, 123)
                self.assertEqual(settings.allowed_user_ids, [456, 789])
                self.assertFalse(settings.mail.enabled)
                self.assertNotIn(token, (root / 'data' / 'config.json').read_text())

    def test_cancel_does_not_write_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('infobot.setup.ROOT', root), patch('infobot.setup.getpass', return_value='unused'), \
                    patch('builtins.input', side_effect=['', '', 'n']), patch('builtins.print'), \
                    patch.dict('os.environ', {'INFOBOT_TOKEN': ''}):
                self.assertFalse(configure())
            self.assertFalse((root / 'data').exists())
