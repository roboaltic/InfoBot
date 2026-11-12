#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

from infobot.settings import ROOT, Settings, load_token


class UkrainianHelp(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or 'Використання: ')


class UkrainianParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, 'Помилка параметрів запуску. Перегляньте довідку: main.py --help\n')


def main():
    parser = UkrainianParser(description="InfoBot: локальний інформаційний Telegram-бот", add_help=False, formatter_class=UkrainianHelp)
    parser._positionals.title = 'Позиційні аргументи'
    parser._optionals.title = 'Параметри'
    parser.add_argument('-h', '--help', action='help', help='Показати цю довідку та вийти')
    parser.add_argument("--demo", action="store_true", help="Відкрити автономний інтерфейс із підробленим Telegram без токена та мережі")
    parser.add_argument("--console", action="store_true", help="Запустити без графічного вікна")
    parser.add_argument("--setup", action="store_true", help="Заповнити форму в терміналі та запустити бота без графічного вікна")
    parser.add_argument("--self-test", action="store_true", help="Запустити автономні тести; Telegram і SMTP не використовуються")
    args = parser.parse_args()
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.setup:
        from infobot.setup import configure
        try:
            if not configure():
                return 0
        except (ValueError, OSError, EOFError, KeyboardInterrupt):
            print('\nНалаштування не завершено. Перевірте токен, числові ID та доступ до папки data.')
            return 1
    if args.console or args.setup:
        from infobot.runner import Runner
        try:
            settings = Settings.load(ROOT / "data" / "config.json")
            token = load_token(ROOT / "data" / "token.secret")
        except (ValueError, OSError):
            print("Не вдалося прочитати data/config.json або токен. Налаштуйте програму через вікно.")
            return 1
        if not token:
            print("Спочатку відкрийте вікно, вставте токен і натисніть «Зберегти й запустити».")
            return 1
        failed = []
        def emit(kind, value):
            if kind == "pairing":
                print("Одноразова команда підключення спеціаліста (10 хвилин): /claim " + value, flush=True)
            elif kind in ("log", "error", "running", "operator"):
                label = {"log": "Журнал", "error": "Помилка", "running": "Працює", "operator": "Спеціаліст"}[kind]
                print(f"[{label}] {value}", flush=True)
            if kind == "error":
                failed.append(True)
        runner = Runner(token, settings, emit)
        runner.start()
        try:
            while runner.thread.is_alive():
                runner.thread.join(0.25)
        except KeyboardInterrupt:
            print("Зупинка. Дочекайтеся завершення поточних запитів Telegram і SMTP…", flush=True)
            runner.stop.set()
            if runner.mail_worker:
                runner.mail_worker.stop.set()
            runner.thread.join()
        return 1 if failed else 0
    try:
        if args.demo:
            import tkinter as tk
            from infobot.demo import Demo
            root = tk.Tk()
            root.title('InfoBot · автономна перевірка')
            root.geometry('1100x850')
            Demo(root).pack(fill='both', expand=True)
            root.mainloop()
            return 0
        from infobot.gui import launch
        launch(args.demo)
    except ImportError:
        show_startup_error("Не знайдено tkinter. Установіть звичайний Python із Tcl/Tk; у Linux потрібен пакет python3-tk.")
        return 1
    except (ValueError, OSError) as error:
        show_startup_error("Перевірте каталог content, faq.json, mail_template.json і доступ до папки програми.\n" + str(error))
        return 1
    return 0


def show_startup_error(message):
    print(message)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "InfoBot: помилка запуску", 0x10)


if __name__ == "__main__":
    sys.exit(main())
