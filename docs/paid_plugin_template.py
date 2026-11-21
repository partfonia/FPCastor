"""Шаблон ПЛАТНОГО плагина Castor (работает только с действующим ключом).

1. Запустите сервер лицензий (license_server/server.py) — он напечатает ОТКРЫТЫЙ ключ.
2. Вставьте его в PUBKEY, адрес сервера — в SERVER, ссылку на магазин — в SHOP.
3. PLUGIN_ID должен совпадать с ключом товара в license_server/data/config.json → products.
4. Перед продажей запутайте файл (см. docs/PAID_PLUGINS.md): pyarmor gen paid_plugin.py
"""
import base64
import json
import time

from castor import licensing
from castor.core import DATA_DIR, fmt_template
from castor.modules import Module

NAME = "Пример платного плагина"
VERSION = "1.0"
DESCRIPTION = "Отвечает на !вип (работает только с ключом)"
CREDITS = "@partfonia"
UUID = "c0ffee00-0000-4000-8000-0000000000bb"      # свой для каждого плагина

PLUGIN_ID = "example_plugin"                        # = ID товара в магазине
SERVER = "http://IP_СЕРВЕРА:ПОРТ"                   # адрес сервера лицензий
PUBKEY = "ВСТАВЬТЕ_ОТКРЫТЫЙ_КЛЮЧ_С_СЕРВЕРА"          # печатается при запуске сервера
SHOP = "https://t.me/ваш_магазин_bot"

licensing.register(PLUGIN_ID, NAME, SERVER, PUBKEY, SHOP)


def _licensed() -> bool:
    """Двойная проверка: общий модуль лицензий + своя проверка подписи внутри плагина,
    чтобы нельзя было «отключить» лицензию правкой открытого кода бота."""
    if not licensing.allowed(PLUGIN_ID):
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        st = json.loads((DATA_DIR / "licenses.json").read_text(encoding="utf-8"))
        c = st["cache"][PLUGIN_ID]
        p = c["payload"]
        Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBKEY)).verify(
            base64.b64decode(c["sig"]), json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        return bool(p.get("ok")) and p.get("plugin") == PLUGIN_ID and p.get("machine") == st.get("machine") \
            and time.time() - p.get("ts", 0) < 72 * 3600 and (not p.get("expires") or p["expires"] > time.time())
    except Exception:
        return False


class PaidExample(Module):
    KEY = "paid_example"
    GROUP = "Платные"
    NAME = NAME
    ICON = "💠"
    DESCRIPTION = "Платный модуль. Ключ: меню → «Далее» → «🔑 Лицензии»."
    DEFAULTS = {"text": "Вы VIP, $username! 💠"}
    FIELDS = [("text", "Ответ на !вип", "area")]

    def on_message(self, m) -> bool:
        if (m.text or "").strip().lower() != "!вип":
            return False
        if not _licensed():
            return False          # без ключа плагин просто молчит
        self.reply(m.platform, m.chat_id, fmt_template(self.get("text"), username=m.author), m.account, m.chat_name)
        return True

    def status(self) -> str:
        info = licensing.registry().get(PLUGIN_ID, {})
        return ("🔑 " + info.get("status", "")) if info else ""


CASTOR_MODULE = PaidExample
