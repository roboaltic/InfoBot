from __future__ import annotations

import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class APIError(Exception):
    def __init__(self, code: int = 0, retry_after: int = 0, description: str = ""):
        self.code = code
        self.retry_after = retry_after
        detail = description.lower() if isinstance(description, str) else ""
        self.edit_unchanged = code == 400 and "message is not modified" in detail
        self.edit_unavailable = code == 400 and any(reason in detail for reason in (
            "message to edit not found", "message can't be edited", "message cannot be edited", "message_id_invalid",
        ))
        descriptions = {
            0: "Немає з’єднання з Telegram. Перевірте інтернет, VPN і доступ до api.telegram.org",
            400: "Telegram відхилив запит (400)",
            401: "Неправильний або відкликаний токен. Перевірте його в BotFather",
            403: "Доставку заборонено: користувач міг заблокувати бота (403)",
            409: "Конфлікт (409): бот запущений ще десь або використовує webhook",
            429: "Telegram обмежив частоту запитів. Надсилання буде повторено",
        }
        super().__init__(descriptions.get(code, f"Помилка Telegram API ({code})"))


class TelegramAPI:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._ssl = ssl.create_default_context()

    def call(self, method: str, payload: dict | None = None):
        data = json.dumps(payload or {}, ensure_ascii=False).encode()
        request = Request(self._base + method, data=data, headers={"Content-Type": "application/json"})
        timeout = int((payload or {}).get("timeout", 0)) + 10
        try:
            with urlopen(request, timeout=timeout, context=self._ssl) as response:
                result = json.load(response)
        except HTTPError as error:
            retry_after = 0
            description = ""
            try:
                detail = json.loads(error.read())
                description = detail.get("description", "")
                retry_after = int(detail.get("parameters", {}).get("retry_after", 0))
            except (AttributeError, TypeError, ValueError, OSError):
                pass
            raise APIError(error.code, retry_after, description) from None
        except (URLError, OSError, socket.timeout, ValueError):
            # Never include urllib exception text: its URL contains the bot token.
            raise APIError() from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 0),
                           result.get("description", ""))
        return result.get("result")
