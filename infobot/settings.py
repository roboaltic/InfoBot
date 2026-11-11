from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from email.headerregistry import Address
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATTERN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}\Z")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_address(value: str) -> None:
    if not isinstance(value, str) or not value or not value.isascii() or any(c in value for c in "\r\n ,;<>\t"):
        raise ValueError("Укажіть одну адресу електронної пошти латиницею без пробілів")
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError):
        raise ValueError("Некоректна адреса електронної пошти") from None
    if not address.username or not address.domain or len(value) > 254:
        raise ValueError("Некоректна адреса електронної пошти")


@dataclass
class MailSettings:
    enabled: bool = False
    host: str = ""
    port: int = 465
    security: str = "ssl"
    username: str = ""
    sender: str = ""
    recipient: str = ""

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("mail.enabled має бути true або false")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SMTP-порт має бути числом від 1 до 65535")
        if self.security not in ("ssl", "starttls"):
            raise ValueError("Оберіть захист SMTP: ssl або starttls")
        for value in (self.host, self.username, self.sender, self.recipient):
            if not isinstance(value, str) or any(c in value for c in "\r\n"):
                raise ValueError("Поштові налаштування мають містити звичайний текст без перенесення рядків")
        if self.enabled:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", self.host) or not self.username.strip():
                raise ValueError("Заповніть SMTP-сервер та ім’я користувача поштової скриньки")
            validate_address(self.sender)
            validate_address(self.recipient)


@dataclass
class Settings:
    operator_id: int = 0
    delete_telegram_on_close: bool = True
    allowed_user_ids: list[int] = field(default_factory=list)
    mail: MailSettings = field(default_factory=MailSettings)
    privacy_policy_url: str = ''

    def validate_privacy_url(self):
        value = self.privacy_policy_url
        if not isinstance(value, str):
            raise ValueError('privacy_policy_url має бути HTTPS-посиланням або порожнім рядком.')
        parsed = urlsplit(value)
        if value and (len(value)>1500 or parsed.scheme!='https' or not parsed.hostname or
                      parsed.username or parsed.password or any(char.isspace() for char in value)):
            raise ValueError('privacy_policy_url має бути HTTPS-посиланням без облікових даних.')

    @classmethod
    def load(cls, path: Path) -> "Settings":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("config.json має містити JSON-об’єкт")
        if "mail" in data:
            if not isinstance(data["mail"], dict):
                raise ValueError("mail має бути JSON-об’єктом")
            data["mail"] = MailSettings(**{key: data["mail"][key] for key in MailSettings.__dataclass_fields__ if key in data["mail"]})
        result = cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
        if type(result.operator_id) is not int or result.operator_id < 0:
            raise ValueError("operator_id має бути числом >= 0")
        if type(result.delete_telegram_on_close) is not bool:
            raise ValueError("delete_telegram_on_close має бути true або false")
        if not isinstance(result.allowed_user_ids, list) or any(
            type(uid) is not int or uid <= 0 for uid in result.allowed_user_ids
        ):
            raise ValueError("allowed_user_ids має містити додатні Telegram ID")
        result.mail.validate()
        result.validate_privacy_url()
        return result

    def save(self, path: Path) -> None:
        self.validate_privacy_url()
        atomic_write(path, (json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n").encode())


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """Windows encryption tied to the current Windows user, with no UI prompts."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    if decrypt:
        function = crypt32.CryptUnprotectData
        description = ctypes.c_void_p()
        function.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(ctypes.c_void_p),
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(Blob)]
        second = ctypes.byref(description)
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(Blob)]
        second = "InfoBot: локальні облікові дані"
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), second, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("Windows не вдалося захистити або прочитати облікові дані поточного користувача")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)
        if decrypt and description.value:
            kernel32.LocalFree(description)


def save_token(path: Path, token: str) -> None:
    token = token.strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Вставте повний токен, виданий BotFather")
    save_secret(path, token)


def save_secret(path: Path, secret: str) -> None:
    if os.name == "nt":
        content = b"dpapi-v1\n" + _dpapi(secret.encode())
    else:
        content = b"plain-v1\n" + secret.encode()
    atomic_write(path, content)
    if os.name != "nt":
        path.chmod(0o600)


def load_token(path: Path) -> str:
    return load_secret(path, "INFOBOT_TOKEN").strip()


def load_secret(path: Path, environment_key: str = "") -> str:
    environment = os.environ.get(environment_key, "") if environment_key else ""
    if environment:
        return environment
    if not path.exists():
        return ""
    kind, data = path.read_bytes().split(b"\n", 1)
    if kind == b"dpapi-v1" and os.name == "nt":
        return _dpapi(data, decrypt=True).decode()
    if kind == b"plain-v1":
        return data.decode()
    raise ValueError("Облікові дані захищені іншою системою. Введіть їх знову на цьому комп’ютері")


class InstanceLock:
    """OS lock: automatically released even if the process crashes."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Бот уже запущений із цієї папки. Зупиніть інший примірник") from None
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
            self.stream = None
