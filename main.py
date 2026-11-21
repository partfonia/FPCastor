#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunPay & Playerok Castor — бот-помощник продавца для FunPay и Playerok.

  python main.py                  меню запуска (мастер при первом запуске)
  python main.py --setup          мастер настройки
  python main.py --no-dialog      запуск без меню (для сервисов / Docker)
  python main.py --install-service  автозапуск (systemd / автозагрузка Windows)
  python main.py --no-install     не проверять зависимости
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

if sys.version_info < (3, 9):
    print("Нужен Python 3.9+ (для Playerok — 3.11+). Скачайте: https://www.python.org/downloads/")
    sys.exit(1)

from castor.bootstrap import ensure_dependencies  # noqa: E402

if "--no-install" not in sys.argv:
    ensure_dependencies()

from castor.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
