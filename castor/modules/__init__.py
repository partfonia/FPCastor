"""Встроенные модули Castor: аренда, коды с почты, автоцена и т.д.

Модуль — класс с полями KEY/NAME/ICON/DESCRIPTION, настройками по умолчанию (DEFAULTS) и
описанием полей (FIELDS) — по нему Telegram и панель сами рисуют экран настроек.
Хуки (все необязательные):
    on_message(m) -> bool   вернуть True, если сообщение обработано (дальше автоответчик не трогает)
    on_order(o)   -> bool   вернуть True, если заказ выдан модулем (обычная автовыдача пропускается)
    tick()                  раз в ~30 секунд
    status()      -> str    строка состояния для меню
    actions()     -> list   [(id, «подпись»)] — кнопки действий в меню; run_action(id, args) их выполняет
Настройки лежат в config.json → modules.<KEY>, данные — в castor_data/modules/<KEY>.json.
"""
from __future__ import annotations

import copy
import threading
import time
import traceback
from typing import TYPE_CHECKING, Optional

from ..core import DATA_DIR, load_json, log, save_json

if TYPE_CHECKING:
    from ..app import App

MOD_DIR = DATA_DIR / "modules"


class Module:
    KEY = ""
    NAME = ""
    ICON = "🧩"
    DESCRIPTION = ""
    PLATFORMS = ("funpay", "playerok")
    DEFAULTS: dict = {}
    # (путь внутри настроек модуля, подпись, тип: bool|text|area|number|lines|select|items, доп.)
    FIELDS: list = []
    ITEM_FIELDS: dict = {}   # для type=items: {путь: [(ключ, подпись, тип, доп.), ...]}

    def __init__(self, app: "App"):
        self.app = app
        self.lock = threading.RLock()
        MOD_DIR.mkdir(parents=True, exist_ok=True)
        self.path = MOD_DIR / f"{self.KEY}.json"
        self.data = load_json(self.path, {})

    # ── настройки и данные ──
    @property
    def cfg(self) -> dict:
        return self.app.cfg.get(f"modules.{self.KEY}", {}) or {}

    def get(self, key, default=None):
        cur = self.cfg
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def save(self):
        with self.lock:
            save_json(self.path, self.data)

    # ── удобства ──
    def reply(self, platform, chat_id, text, account=None, name=None):
        return self.app.send(platform, chat_id, text, name, account)

    def notify(self, text, buttons=None, kind="delivery"):
        self.app.notify(kind, text, buttons=buttons)

    # ── хуки по умолчанию ──
    def on_message(self, m) -> bool:
        return False

    def on_order(self, o) -> bool:
        return False

    def tick(self):
        pass

    def status(self) -> str:
        return ""

    def actions(self) -> list:
        return []

    def run_action(self, action: str, args: list) -> str:
        return "Нет такого действия"


class ModuleManager:
    def __init__(self, app: "App"):
        self.app = app
        from .rent import RentModule
        from .mailcode import MailCodeModule
        from .overdue import OverdueModule
        from .autoprice import AutoPriceModule
        from .copylots import CopyLotsModule
        from .lotschedule import LotScheduleModule
        self.modules: dict = {}
        for cls in (RentModule, MailCodeModule, OverdueModule, AutoPriceModule, CopyLotsModule, LotScheduleModule):
            try:
                self.modules[cls.KEY] = cls(app)
            except Exception as e:  # модуль не должен ронять бота
                log.error("Модуль %s не загрузился: %s", cls.KEY, e)
        self._ensure_defaults()

    def _ensure_defaults(self):
        mods = self.app.cfg.data.setdefault("modules", {})
        changed = False
        for key, m in self.modules.items():
            cur = mods.get(key)
            if not isinstance(cur, dict):
                mods[key] = copy.deepcopy(dict({"enabled": False}, **m.DEFAULTS))
                changed = True
            else:
                for k, v in m.DEFAULTS.items():
                    if k not in cur:
                        cur[k] = copy.deepcopy(v)
                        changed = True
        if changed:
            self.app.cfg.save()

    def register(self, cls) -> bool:
        """Модуль из плагина (CASTOR_MODULE = Класс): появится в меню «Модули» рядом со встроенными."""
        try:
            if not cls.KEY or cls.KEY in self.modules:
                log.warning("Модуль из плагина пропущен: ключ «%s» пустой или занят", cls.KEY)
                return False
            self.modules[cls.KEY] = cls(self.app)
            self._ensure_defaults()
            log.info("🧩 Модуль из плагина: %s", cls.NAME)
            return True
        except Exception as e:
            log.error("Модуль из плагина %s не загрузился: %s", getattr(cls, "KEY", "?"), e)
            return False

    def active(self):
        return [m for m in self.modules.values() if m.enabled]

    def _safe(self, m, fn, *a):
        try:
            return getattr(m, fn)(*a)
        except Exception as e:
            log.warning("Модуль «%s»: %s", m.NAME, e)
            log.debug(traceback.format_exc())
            return None

    def on_message(self, msg) -> bool:
        for m in self.active():
            if msg.platform in m.PLATFORMS and self._safe(m, "on_message", msg):
                return True
        return False

    def on_order(self, o) -> bool:
        for m in self.active():
            if o.platform in m.PLATFORMS and self._safe(m, "on_order", o):
                return True
        return False

    def tick(self):
        for m in self.active():
            self._safe(m, "tick")

    def get(self, key) -> Optional[Module]:
        return self.modules.get(key)

    def describe(self) -> list:
        out = []
        for key, m in self.modules.items():
            out.append({"key": key, "name": m.NAME, "icon": m.ICON, "description": m.DESCRIPTION,
                        "enabled": m.enabled, "status": self._safe(m, "status") or "",
                        "platforms": list(m.PLATFORMS), "fields": m.FIELDS, "item_fields": m.ITEM_FIELDS,
                        "actions": self._safe(m, "actions") or []})
        return out


def now() -> float:
    return time.time()
