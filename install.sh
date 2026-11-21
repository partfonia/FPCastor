#!/usr/bin/env bash
# Установка FunPay & Playerok Castor на Linux / macOS
set -e
cd "$(dirname "$0")"
PY=$(command -v python3.12 || command -v python3.11 || command -v python3 || true)
if [ -z "$PY" ]; then
  echo "Python не найден. Ubuntu/Debian: sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
echo "▶ Python: $($PY --version)"
if [ ! -d venv ]; then
  $PY -m venv venv || { echo "Нужен пакет python3-venv: sudo apt install -y python3-venv"; exit 1; }
fi
./venv/bin/python -m pip install -U pip >/dev/null
./venv/bin/python -m pip install -r requirements.txt
chmod +x start.sh
echo
echo "✔ Готово. Запуск: ./start.sh   (первый запуск откроет мастер настройки)"
echo "  Автозапуск как сервис: ./venv/bin/python main.py --install-service"
