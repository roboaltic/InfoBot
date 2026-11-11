from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .settings import InstanceLock, atomic_write

MAX_ITEMS = 60


class FAQConflict(ValueError):
    pass


def size(value: str) -> int:
    return len(value.encode('utf-16-le')) // 2


def validate_question(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or size(value.strip()) > 60 or any(c in value for c in '\r\n'):
        raise ValueError('Запитання: один рядок, від 1 до 60 одиниць UTF-16.')
    return value.strip()


def validate_answer(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or size(value.strip()) > 3000:
        raise ValueError('Відповідь має містити від 1 до 3000 одиниць UTF-16.')
    return value.strip()


def validate_source(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Джерело має бути текстовим HTTPS-посиланням.')
    if not value or value.strip() in ('-', '—'):
        return ''
    value = value.strip()
    parsed = urlsplit(value)
    if len(value) > 1500 or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):
        raise ValueError('Джерело має бути HTTPS-посиланням без пробілів; для відсутнього джерела введіть —.')
    return value


def validate_items(items) -> list[dict]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError(f'Каталог FAQ має містити від 1 до {MAX_ITEMS} запитань.')
    result, identifiers = [], set()
    for original in items:
        if not isinstance(original, dict) or not isinstance(original.get('id'), str):
            raise ValueError('Кожне FAQ має містити id, question та answer.')
        item = copy.deepcopy(original)
        identifier = item['id']
        if not re.fullmatch(r'[a-z0-9_-]{1,24}', identifier) or identifier in identifiers:
            raise ValueError('id запитання має бути унікальним: латиниця, цифри, _ або -, до 24 знаків.')
        item['question'] = validate_question(item.get('question'))
        item['answer'] = validate_answer(item.get('answer'))
        item['source_url'] = validate_source(item.get('source_url', ''))
        for key in ('verified_at', 'updated_at'):
            if key in item and (not isinstance(item[key], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', item[key])):
                raise ValueError('Дата в FAQ має відповідати формату РРРР-ММ-ДД.')
        identifiers.add(identifier)
        result.append(item)
    return result


def load_faq(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    return validate_items(raw['items'] if isinstance(raw, dict) and 'items' in raw else raw)


class FAQCatalog:
    """One validated snapshot shared by the GUI and Telegram worker."""
    def __init__(self, items: list[dict], path: Path | None = None):
        self._lock = threading.RLock()
        self.path = path
        self._revision = 0
        self._items = validate_items(items)
        if path and path.exists():
            self.reload()

    @classmethod
    def open(cls, root: Path):
        return cls(load_faq(root / 'faq.json'), root / 'data' / 'faq_catalog.json')

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._items), self._revision

    def get(self, identifier: str):
        with self._lock:
            return copy.deepcopy(next((item for item in self._items if item['id'] == identifier), None))

    def reload(self):
        with self._lock:
            if not self.path or not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or type(data.get('revision')) is not int or data['revision'] < 0:
                raise ValueError('Перевірте структуру data/faq_catalog.json.')
            items = validate_items(data.get('items'))
            self._items, self._revision = items, data['revision']

    def _commit(self, items: list[dict], expected_revision: int):
        items = validate_items(items)
        if expected_revision != self._revision:
            raise FAQConflict('FAQ уже змінено. Оновіть список і повторіть редагування.')
        revision = self._revision + 1
        data = {'schema': 1, 'revision': revision, 'items': items}
        if self.path:
            with InstanceLock(self.path.with_suffix('.lock')):
                if self.path.exists():
                    on_disk = json.loads(self.path.read_text(encoding='utf-8-sig'))
                    if not isinstance(on_disk, dict) or type(on_disk.get('revision')) is not int:
                        raise ValueError('Перевірте структуру data/faq_catalog.json. Зміни не збережено.')
                    if on_disk.get('revision') != expected_revision:
                        raise FAQConflict('FAQ змінено в іншому вікні. Оновіть список перед збереженням.')
                elif expected_revision != 0:
                    raise FAQConflict('Файл FAQ було змінено або переміщено. Оновіть список.')
                atomic_write(self.path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
        self._items, self._revision = items, revision

    def add(self, item: dict, expected_revision: int):
        with self._lock:
            candidate = copy.deepcopy(item)
            candidate['updated_at'] = datetime.now(timezone.utc).date().isoformat()
            self._commit(self._items + [candidate], expected_revision)

    def update(self, identifier: str, values: dict, expected_revision: int):
        with self._lock:
            if not set(values) <= {'question', 'answer', 'source_url'}:
                raise ValueError('Недопустимі поля редагування FAQ.')
            items = copy.deepcopy(self._items)
            item = next((item for item in items if item['id'] == identifier), None)
            if item is None:
                raise FAQConflict('Це запитання вже видалено. Оновіть список.')
            if ('answer' in values and values['answer'] != item['answer']) or ('source_url' in values and values['source_url'] != item.get('source_url', '')):
                # A manual answer edit does not imply a fresh source verification.
                item.pop('verified_at', None)
                if 'source_url' not in values:
                    item['source_url'] = ''
            item.update(values)
            item['updated_at'] = datetime.now(timezone.utc).date().isoformat()
            self._commit(items, expected_revision)

    def delete(self, identifier: str, expected_revision: int):
        with self._lock:
            items = [item for item in self._items if item['id'] != identifier]
            if len(items) == len(self._items):
                raise FAQConflict('Це запитання вже видалено. Оновіть список.')
            self._commit(items, expected_revision)

    def backup(self, path: Path):
        items, revision = self.snapshot()
        data = {'schema': 1, 'revision': revision, 'items': items}
        atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
