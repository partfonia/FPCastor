"""Лицензии платных плагинов.

Платный плагин регистрирует себя (register) и спрашивает check(). Ключ покупатель вводит в боте
(«🔑 Лицензии» в меню или в панели). Сервер лицензий отвечает подписанным ответом (Ed25519):
подделать ответ без закрытого ключа продавца нельзя. Ключ привязан к Telegram ID покупателя и к одной
установке бота. Без связи с сервером плагин работает до 72 часов по последнему подтверждённому ответу.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from typing import Optional

import requests

from .core import DATA_DIR, load_json, log, save_json

STATE_PATH = DATA_DIR / "licenses.json"
GRACE_SEC = 72 * 3600
RECHECK_SEC = 6 * 3600

_lock = threading.RLock()
_registry: dict = {}      # plugin_id → {"name", "server", "pubkey", "shop", "status"}
_app = None


def bind(app):
    global _app
    _app = app


def machine_id() -> str:
    """Постоянный ID этой установки бота (лежит в castor_data)."""
    st = load_json(STATE_PATH, {})
    if not st.get("machine"):
        st["machine"] = secrets.token_hex(16)
        save_json(STATE_PATH, st)
    return st["machine"]


def owners() -> list:
    try:
        return [int(u["id"]) for u in _app.team.users() if u.get("role") == "owner"] if _app else []
    except Exception:
        return []


def get_key(plugin_id) -> str:
    return (_app.cfg.get("licenses", {}) or {}).get(plugin_id, "") if _app else ""


def set_key(plugin_id, key: str):
    if not _app:
        return
    lic = dict(_app.cfg.get("licenses", {}) or {})
    lic[plugin_id] = key.strip()
    _app.cfg.set("licenses", lic)
    with _lock:
        if plugin_id in _registry:
            _registry[plugin_id]["checked"] = 0


def register(plugin_id: str, name: str, server: str, pubkey: str, shop: str = "") -> None:
    """Вызывается платным плагином при загрузке."""
    with _lock:
        _registry[plugin_id] = {"name": name, "server": server.rstrip("/"), "pubkey": pubkey, "shop": shop,
                                "status": "ключ не введён" if not get_key(plugin_id) else "проверяю…",
                                "ok": False, "expires": 0, "checked": 0}


def registry() -> dict:
    return _registry


def verify(payload: dict, sig_b64: str, pubkey_b64: str) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pk.verify(base64.b64decode(sig_b64), canonical(payload))
        return True
    except Exception:
        return False


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def check(plugin_id: str, force=False) -> tuple:
    """→ (можно ли работать, понятное пояснение). Результат кешируется на 6 часов."""
    reg = _registry.get(plugin_id)
    if not reg:
        return False, "плагин не зарегистрирован"
    if not force and time.time() - reg.get("checked", 0) < RECHECK_SEC and reg.get("checked"):
        return reg["ok"], reg["status"]
    key = get_key(plugin_id)
    if not key:
        _set(plugin_id, False, "ключ не введён", 0)
        return False, "ключ не введён"
    nonce = secrets.token_hex(12)
    body = {"key": key, "plugin": plugin_id, "machine": machine_id(), "owners": owners(), "nonce": nonce}
    try:
        r = requests.post(reg["server"] + "/api/check", json=body, timeout=15)
        j = r.json()
        payload, sig = j.get("payload") or {}, j.get("sig", "")
        if not verify(payload, sig, reg["pubkey"]) or payload.get("nonce") != nonce \
                or payload.get("plugin") != plugin_id or payload.get("machine") != machine_id():
            raise ValueError("ответ сервера не прошёл проверку подписи")
        st = load_json(STATE_PATH, {})
        st.setdefault("cache", {})[plugin_id] = {"payload": payload, "sig": sig}
        save_json(STATE_PATH, st)
        ok = bool(payload.get("ok"))
        _set(plugin_id, ok, _describe(payload), payload.get("expires", 0))
        return ok, _registry[plugin_id]["status"]
    except Exception as e:
        # нет связи — работаем по последнему подписанному ответу не дольше 72 часов
        cache = (load_json(STATE_PATH, {}).get("cache") or {}).get(plugin_id)
        if cache and verify(cache["payload"], cache["sig"], reg["pubkey"]):
            p = cache["payload"]
            if p.get("ok") and p.get("machine") == machine_id() and time.time() - p.get("ts", 0) < GRACE_SEC \
                    and (not p.get("expires") or p["expires"] > time.time()):
                _set(plugin_id, True, "нет связи с сервером лицензий — работаю по последней проверке", p.get("expires", 0))
                return True, _registry[plugin_id]["status"]
        log.warning("🔑 Лицензия %s: %s", plugin_id, e)
        _set(plugin_id, False, "не удалось проверить ключ — нет связи с сервером лицензий", 0)
        return False, _registry[plugin_id]["status"]


def _describe(p: dict) -> str:
    if not p.get("ok"):
        return p.get("reason") or "ключ недействителен"
    exp = p.get("expires") or 0
    if not exp:
        return "активна навсегда"
    days = max(0, int((exp - time.time()) // 86400))
    return f"активна до {time.strftime('%d.%m.%Y', time.localtime(exp))} (ещё {days} дн.)"


def _set(plugin_id, ok, status, expires):
    with _lock:
        if plugin_id in _registry:
            _registry[plugin_id].update(ok=ok, status=status, expires=expires, checked=time.time())


def allowed(plugin_id: str) -> bool:
    """Быстрая проверка для хуков плагина (без сети, кроме раз в 6 часов)."""
    return check(plugin_id)[0]


def describe() -> list:
    return [{"id": pid, "name": r["name"], "ok": r["ok"], "status": r["status"], "shop": r["shop"],
             "has_key": bool(get_key(pid))} for pid, r in _registry.items()]
