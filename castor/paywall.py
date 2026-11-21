"""Подписка «Все плагины Castor»: встроенные плагины работают при активном ключе.

Пока SERVER и PUBKEY пустые, подписка выключена и все плагины работают бесплатно.
Заполняет их владелец командой: python tools/setup_paywall.py --server http://IP:ПОРТ --pubkey КЛЮЧ --shop https://t.me/магазин
Плагины, установленные пользователем из файлов, подписка не затрагивает.
"""
from __future__ import annotations

PRODUCT_ID = "castor_plugins"
NAME = "Все плагины Castor"
PRICE = "50 ₽ в месяц"

# ↓ заполняется tools/setup_paywall.py
SERVER = ""
PUBKEY = ""
SHOP = ""
# ↑


def configured() -> bool:
    return bool(SERVER and PUBKEY)


def init():
    if configured():
        from . import licensing
        licensing.register(PRODUCT_ID, NAME, SERVER, PUBKEY, SHOP)


def unlocked() -> bool:
    """Можно ли сейчас работать встроенным плагинам (проверка кешируется на 6 часов)."""
    if not configured():
        return True
    from . import licensing
    return licensing.allowed(PRODUCT_ID)


def info() -> dict:
    """Для меню и панели."""
    if not configured():
        return {"enabled": False, "ok": True}
    from . import licensing
    reg = licensing.registry().get(PRODUCT_ID) or {}
    return {"enabled": True, "ok": unlocked(), "status": reg.get("status", ""), "id": PRODUCT_ID,
            "name": NAME, "price": PRICE, "shop": SHOP, "has_key": bool(licensing.get_key(PRODUCT_ID))}
