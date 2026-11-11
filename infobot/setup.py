"""Interactive console setup for machines without Tkinter."""
from __future__ import annotations

from getpass import getpass

from .settings import ROOT, Settings, load_token, save_token


def configure() -> bool:
    print('InfoBot — налаштування та запуск')
    settings = Settings.load(ROOT / 'data' / 'config.json')
    current_token = load_token(ROOT / 'data' / 'token.secret')
    token = getpass('Токен BotFather (ввід прихований; Enter — збережений токен): ').strip() or current_token
    if not token:
        raise ValueError('Потрібен токен BotFather.')
    raw_operator = input(f'ID диспетчера (Enter — {settings.operator_id}; 0 — підключити через /claim): ').strip()
    if raw_operator:
        settings.operator_id = int(raw_operator)
    if settings.operator_id < 0:
        raise ValueError('ID диспетчера має бути числом >= 0.')
    print('Учасники: Enter — залишити поточний список; * — дозволити всіх.')
    raw_allowed = input('Дозволені Telegram ID через кому: ').strip()
    if raw_allowed == '*':
        settings.allowed_user_ids = []
    elif raw_allowed:
        settings.allowed_user_ids = sorted(set(int(part.strip()) for part in raw_allowed.split(',')))
    if any(uid <= 0 for uid in settings.allowed_user_ids):
        raise ValueError('Telegram ID мають бути додатними числами.')
    print('Пошта зараз ' + ('увімкнена.' if settings.mail.enabled else 'вимкнена.'))
    if input('Зберегти налаштування та запустити бота? [y/N]: ').strip().lower() not in ('y', 'yes', 'так'):
        print('Запуск скасовано. Налаштування не змінено.')
        return False
    save_token(ROOT / 'data' / 'token.secret', token)
    settings.save(ROOT / 'data' / 'config.json')
    # Console startup must use the token entered here, even if an older environment
    # variable was present when this setup form was opened.
    import os
    os.environ['INFOBOT_TOKEN'] = token
    return True
