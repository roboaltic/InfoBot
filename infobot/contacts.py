from __future__ import annotations

import re

from .settings import validate_address

STEPS = ('email', 'phone', 'full_name', 'company')
REQUIRED = ('client_email', 'client_phone', 'client_full_name', 'client_first_name', 'client_company')
PROMPTS = {
    'email': 'Крок 1 із 4. Укажіть свою електронну пошту. Приклад: client@example.com.',
    'phone': 'Крок 2 із 4. Укажіть номер телефону. Приклад: +380671234567. Український номер 0671234567 також приймається.',
    'full_name': 'Крок 3 із 4. Укажіть ПІБ у порядку «Прізвище Ім’я По батькові». Приклад: Шевченко Олена Іванівна. Якщо по батькові немає, замість нього вкажіть —.',
    'company': 'Крок 4 із 4. Укажіть компанію у форматі ТОВ "Укрхелп" — із назвою в лапках.',
}


def complete(row) -> bool:
    return bool(row and all(str(row[key]).strip() for key in REQUIRED))


def normalize_email(text: str) -> str:
    value = text.strip()
    validate_address(value)
    local, domain = value.rsplit('@', 1)
    return local + '@' + domain.lower()


def normalize_phone(text: str) -> str:
    raw = text.strip()
    if not re.fullmatch(r'\+?[0-9 ()\-.]{8,40}', raw):
        raise ValueError('Укажіть номер цифрами з кодом країни, наприклад +380671234567.')
    value = re.sub(r'[ ()\-.]', '', raw)
    if re.fullmatch(r'0[0-9]{9}', value):
        value = '+38' + value
    elif re.fullmatch(r'380[0-9]{9}', value):
        value = '+' + value
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
        raise ValueError('Перевірте номер: потрібен код країни та від 8 до 15 цифр. Приклад: +380671234567.')
    return value


def _name_part(value: str) -> bool:
    if not 2 <= len(value) <= 60 or value[0] in "-'" or value[-1] in "-'":
        return False
    return all(char.isalpha() or char in "-'" for char in value) and all(part.isalpha() for part in re.split("[-']", value))


def normalize_full_name(text: str) -> tuple[str, str]:
    if any(char in text for char in '\r\n'):
        raise ValueError('Напишіть ПІБ одним рядком: Прізвище Ім’я По батькові.')
    value = text.replace('’', "'").replace('ʼ', "'").replace('`', "'").replace('‐', '-').replace('‑', '-')
    parts = value.split()
    if len(parts) != 3 or not all(_name_part(part) for part in parts[:2]) or not (_name_part(parts[2]) or parts[2] in ('—', '-')):
        raise ValueError('Потрібні прізвище, повне ім’я та по батькові, без ініціалів. Приклад: Шевченко Олена Іванівна. Якщо по батькові немає — вкажіть —.')
    if parts[2] == '-':
        parts[2] = '—'
    return ' '.join(parts), parts[1]


def normalize_company(text: str) -> str:
    value = text.strip()
    if len(value) > 180 or any(char in value for char in '\r\n'):
        raise ValueError('Укажіть компанію одним рядком, до 180 знаків, у форматі ТОВ "Укрхелп".')
    match = re.fullmatch(r'(?i:ТОВ)\s+(["«“])(.+)(["»”])', value)
    pairs = {'"': '"', '«': '»', '“': '”'}
    if not match or pairs[match[1]] != match[3]:
        raise ValueError('Потрібен формат ТОВ "Укрхелп": ТОВ, пробіл і назва компанії в парних лапках.')
    name = ' '.join(match[2].split())
    if not name or not any(char.isalnum() for char in name) or any(char in name for char in '"«»“”'):
        raise ValueError('Укажіть непорожню назву компанії в лапках. Приклад: ТОВ "Укрхелп".')
    return 'ТОВ "' + name + '"'


def validate_step(step: str, text: str) -> dict:
    if step == 'email':
        return {'client_email': normalize_email(text)}
    if step == 'phone':
        return {'client_phone': normalize_phone(text)}
    if step == 'full_name':
        full_name, first_name = normalize_full_name(text)
        return {'client_full_name': full_name, 'client_first_name': first_name}
    return {'client_company': normalize_company(text)}


# Common Ukrainian names; unknown names are preserved rather than guessed.
VOCATIVES = dict(pair.split(':', 1) for pair in (
    'Олександр:Олександре Олексій:Олексію Андрій:Андрію Антон:Антоне Артем:Артеме '
    'Артур:Артуре Богдан:Богдане Борис:Борисе Вадим:Вадиме Валентин:Валентине '
    'Валерій:Валерію Василь:Василю Віктор:Вікторе Віталій:Віталію Володимир:Володимире '
    'Владислав:Владиславе Григорій:Григорію Данило:Даниле Денис:Денисе Дмитро:Дмитре '
    'Євген:Євгене Іван:Іване Ігор:Ігорю Ілля:Ілле Кирило:Кириле Костянтин:Костянтине '
    'Леонід:Леоніде Максим:Максиме Марко:Марку Микита:Микито Микола:Миколо '
    'Михайло:Михайле Назар:Назаре Олег:Олеже Павло:Павле Петро:Петре '
    'Роман:Романе Ростислав:Ростиславе Руслан:Руслане Сергій:Сергію Станіслав:Станіславе '
    'Степан:Степане Тарас:Тарасе Юрій:Юрію Ярослав:Ярославе '
    'Аліна:Аліно Алла:Алло Анастасія:Анастасіє Ангеліна:Ангеліно Анна:Анно '
    'Богдана:Богдано Валентина:Валентино Валерія:Валеріє Вікторія:Вікторіє '
    "Галина:Галино Ганна:Ганно Дарина:Дарино Дарія:Даріє Дар'я:Дар'є Діана:Діано Любов:Любове "
    'Євгенія:Євгеніє Єлизавета:Єлизавето Зоя:Зоє Інна:Інно Ірина:Ірино '
    'Катерина:Катерино Лариса:Ларисо Лілія:Ліліє Людмила:Людмило '
    'Марина:Марино Марія:Маріє Надія:Надіє Наталія:Наталіє Ніна:Ніно '
    'Оксана:Оксано Олена:Олено Ольга:Ольго Поліна:Поліно Раїса:Раїсо '
    'Світлана:Світлано Софія:Софіє Таїсія:Таїсіє Тамара:Тамаро Тетяна:Тетяно '
    'Христина:Христино Юлія:Юліє Яна:Яно'
).split())
VOCATIVES = {key.casefold(): value for key, value in VOCATIVES.items()}


def address_name(name: str) -> str:
    parts = name.split('-')
    if all(part.casefold() in VOCATIVES for part in parts):
        return '-'.join(VOCATIVES[part.casefold()] for part in parts)
    return name.capitalize() if name.isupper() else name
