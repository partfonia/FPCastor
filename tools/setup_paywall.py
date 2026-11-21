"""Включает подписку на встроенные плагины: вписывает адрес сервера лицензий и открытый ключ в castor/paywall.py.

    python tools/setup_paywall.py --server http://IP:ПОРТ --pubkey ОТКРЫТЫЙ_КЛЮЧ --shop https://t.me/ваш_магазин_bot

Открытый ключ сервер лицензий печатает при запуске (его можно публиковать — подделать ключи им нельзя).
После этого залейте бота на GitHub/хостинг. Чтобы снова сделать всё бесплатным: --off
"""
import argparse
import re
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "castor" / "paywall.py"


def put(src, name, value):
    return re.sub(rf'^{name} = ".*"$', f'{name} = {value!r}'.replace("'", '"'), src, count=1, flags=re.M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="")
    ap.add_argument("--pubkey", default="")
    ap.add_argument("--shop", default="")
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()
    src = P.read_text(encoding="utf-8")
    if a.off:
        a.server = a.pubkey = a.shop = ""
    elif not (a.server and a.pubkey):
        ap.error("нужны --server и --pubkey (или --off)")
    src = put(src, "SERVER", a.server.rstrip("/"))
    src = put(src, "PUBKEY", a.pubkey.strip())
    src = put(src, "SHOP", a.shop.strip())
    P.write_text(src, encoding="utf-8")
    print("Подписка выключена — все плагины бесплатны." if a.off else
          f"Готово: подписка включена.\n  сервер: {a.server}\n  магазин: {a.shop or '—'}")


if __name__ == "__main__":
    main()
