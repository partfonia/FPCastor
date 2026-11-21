"""Менеджер плагинов: формат FunPay Cardinal, формат Castor и модули Playerok Universal."""
from __future__ import annotations

import html
import importlib
import importlib.util
import re
import shutil  # noqa: F401
import sys
import traceback
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import requests

from .bootstrap import pip_install as _pip_install
from .core import PLUGINS_DIR, log
from .compat import (_Anything, _StubModule, CBT, EventTypes, NewMessageEvent, NewOrderEvent,
                     OrderStatusChangedEvent, LastChatMessageChangedEvent, StubLocalizer,
                     CardinalCompat, PlayerokCompatBot)
from .funpay import (FunPayClient, FunPayError, FPMessage, FPOrderShortcut, FPOrder, FPReview,
                     MessageTypes, OrderStatuses, SubCategoryTypes)

if TYPE_CHECKING:
    from .app import App

class PluginInfo:
    def __init__(self, path: Path, module, kind: str):
        self.path, self.module, self.kind = path, module, kind
        m = module
        self.name = getattr(m, "NAME", path.stem)
        self.version = getattr(m, "VERSION", "?")
        self.description = getattr(m, "DESCRIPTION", "")
        self.credits = getattr(m, "CREDITS", getattr(m, "AUTHORS", ""))
        self.uuid = str(getattr(m, "UUID", "") or uuid.uuid5(uuid.NAMESPACE_URL, str(path.name)))
        self.settings_page = bool(getattr(m, "SETTINGS_PAGE", False))
        self.commands = []
        self.error = None
        self.hooks: dict = {}
        for attr in dir(m):
            if attr.startswith("BIND_TO_"):
                val = getattr(m, attr)
                if isinstance(val, list):
                    self.hooks[attr] = val
        # Playerok Universal: PLAYEROK_EVENT_HANDLERS = {EventTypes.X: [f,...]}
        for key, val in (getattr(m, "PLAYEROK_EVENT_HANDLERS", None) or {}).items():
            name = getattr(key, "name", str(key))
            self.hooks.setdefault("BIND_TO_PL_" + name, []).extend(val if isinstance(val, list) else [val])
        for key, val in (getattr(m, "BOT_EVENT_HANDLERS", None) or {}).items():
            if str(key) in ("ON_PLAYEROK_BOT_INIT",):
                self.hooks.setdefault("BIND_TO_PL_BOT_INIT", []).extend(val if isinstance(val, list) else [val])
            elif str(key) in ("ON_INIT",):
                self.hooks.setdefault("BIND_TO_PU_INIT", []).extend(val if isinstance(val, list) else [val])

    def to_dict(self, enabled):
        return {"uuid": self.uuid, "name": self.name, "version": self.version, "description": self.description,
                "credits": self.credits, "file": self.path.name, "enabled": enabled, "kind": self.kind,
                "settings": self.settings_page, "hooks": sorted(self.hooks), "commands": self.commands}


class PluginManager:
    def __init__(self, app: "App"):
        self.app = app
        self.plugins: dict[str, PluginInfo] = {}
        self.failed: list = []

    def enabled(self, uid) -> bool:
        return uid not in self.app.cfg.get("plugins.disabled", [])

    def toggle(self, uid) -> bool:
        dis = list(self.app.cfg.get("plugins.disabled", []))
        if uid in dis:
            dis.remove(uid)
        else:
            dis.append(uid)
        self.app.cfg.set("plugins.disabled", dis)
        return uid not in dis

    def install_shims(self):
        from .telegram_ui import TelegramUI
        cardinal = self.app.cardinal
        fp_types = {"Message": FPMessage, "OrderShortcut": FPOrderShortcut, "Order": FPOrder,
                    "Review": FPReview, "MessageTypes": MessageTypes, "OrderStatuses": OrderStatuses,
                    "SubCategoryTypes": SubCategoryTypes}
        ev = {"NewMessageEvent": NewMessageEvent, "NewOrderEvent": NewOrderEvent,
              "OrderStatusChangedEvent": OrderStatusChangedEvent,
              "LastChatMessageChangedEvent": LastChatMessageChangedEvent, "EventTypes": EventTypes}
        enums = {"MessageTypes": MessageTypes, "OrderStatuses": OrderStatuses,
                 "SubCategoryTypes": SubCategoryTypes, "EventTypes": EventTypes}
        exc = {"RequestFailedError": FunPayError, "UnauthorizedError": FunPayError,
               "MessageNotDeliveredError": FunPayError, "RaiseError": FunPayError, "FeedbackEditingError": FunPayError,
               "RefundError": FunPayError}

        def escape(t):
            return html.escape(str(t), quote=False)

        def bool_to_text(v, on="🟢", off="🔴"):
            return on if v else off

        def clear_state_btn():
            from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("❌ Отмена", callback_data=CBT.CLEAR_STATE))
            return kb

        stubs = {
            "FunPayAPI": {"Account": FunPayClient, "types": None, "enums": None, "Runner": _Anything},
            "FunPayAPI.account": {"Account": FunPayClient},
            "FunPayAPI.types": fp_types,
            "FunPayAPI.updater": {},
            "FunPayAPI.updater.events": ev,
            "FunPayAPI.updater.runner": {"Runner": _Anything},
            "FunPayAPI.common": {},
            "FunPayAPI.common.enums": enums,
            "FunPayAPI.common.exceptions": exc,
            "FunPayAPI.common.utils": {"RegularExpressions": _Anything},
            "cardinal": {"Cardinal": CardinalCompat},
            "tg_bot": {"CBT": CBT, "utils": None, "keyboards": None, "static_keyboards": None},
            "tg_bot.CBT": {k: v for k, v in vars(CBT).items() if not k.startswith("_")},
            "tg_bot.utils": {"escape": escape, "bool_to_text": bool_to_text},
            "tg_bot.keyboards": {},
            "tg_bot.static_keyboards": {"CLEAR_STATE_BTN": clear_state_btn},
            "tg_bot.bot": {"TGBot": TelegramUI},
            "Utils": {},
            "Utils.cardinal_tools": {"cache_blacklist": lambda *a, **k: None},
            "Utils.config_loader": {},
            "locales": {},
            "locales.localizer": {"Localizer": StubLocalizer},
            # Playerok Universal
            "core": {}, "core.modules_manager": {"Module": _Anything, "disable_module": lambda *a, **k: None},
            "core.modules": {"Module": _Anything, "disable_module": lambda *a, **k: None,
                             "enable_module": lambda *a, **k: None, "reload_module": lambda *a, **k: None},
            "plbot": {}, "plbot.playerokbot": {"PlayerokBot": PlayerokCompatBot, "get_playerok_bot": lambda: self.app.pl_compat},
            "settings": {"Settings": _Anything()},
        }
        for name, attrs in stubs.items():
            top = name.split(".")[0]
            try:
                real = importlib.util.find_spec(top) is not None and top not in ("cardinal", "tg_bot")
            except (ImportError, ValueError):
                real = False
            if real and top in ("Utils", "locales", "core", "plbot", "settings"):
                continue
            if name in sys.modules and not isinstance(sys.modules[name], _StubModule):
                continue
            mod = _StubModule(name, attrs)
            sys.modules[name] = mod
            if "." in name:
                parent, child = name.rsplit(".", 1)
                if parent in sys.modules:
                    setattr(sys.modules[parent], child, mod)
        # cardinal.Cardinal должен быть реальным классом для isinstance/TYPE_CHECKING
        sys.modules["cardinal"].cardinal = cardinal

    def load_all(self):
        self.install_shims()
        for p in sorted(PLUGINS_DIR.iterdir()):
            if p.name.startswith(("_", ".")):
                continue
            if p.is_file() and p.suffix == ".py":
                self.load(p)
            elif p.is_dir() and (p / "__init__.py").exists():
                self.load(p / "__init__.py", folder=p)

    def load(self, path: Path, folder: Optional[Path] = None) -> Optional[PluginInfo]:
        modname = "duo_plugin_" + re.sub(r"\W", "_", (folder or path).stem)
        self.failed = [f for f in self.failed if f["file"] != (folder or path).name]
        try:
            req = (folder or path.parent) / "requirements.txt"
            if folder and req.exists():
                for line in req.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pkg = re.split(r"[<>=\[ ]", line)[0]
                        if importlib.util.find_spec(pkg.replace("-", "_").lower()) is None:
                            _pip_install(line)
            spec = importlib.util.spec_from_file_location(
                modname, path, submodule_search_locations=[str(folder)] if folder else None)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            kind = "playerok-universal" if hasattr(mod, "PLAYEROK_EVENT_HANDLERS") else (
                "duo" if any(a.startswith(("BIND_TO_ANY", "BIND_TO_PL_")) for a in dir(mod)) else "cardinal")
            info = PluginInfo(path if not folder else folder, mod, kind)
            if info.uuid in self.plugins:
                log.warning("Плагин %s: UUID уже занят — пропущен", info.name)
                return None
            self.plugins[info.uuid] = info
            log.info("🧩 Плагин загружен: %s v%s (%s)", info.name, info.version, kind)
            return info
        except Exception as e:
            log.error("🧩 Ошибка загрузки плагина %s: %s", path.name, e)
            log.debug(traceback.format_exc())
            self.failed.append({"file": (folder or path).name, "error": str(e)})
            return None

    def call(self, hook: str, *args, uid: Optional[str] = None):
        for info in list(self.plugins.values()):
            if (uid and info.uuid != uid) or not self.enabled(info.uuid):
                continue
            for fn in info.hooks.get(hook) or []:
                if not callable(fn):
                    continue
                try:
                    fn(*args)
                except Exception as e:
                    log.error("🧩 %s → %s: %s", info.name, hook, e)
                    log.debug(traceback.format_exc())

    def call_pl(self, hook: str, ev):
        """Playerok-события: модулям Playerok Universal — (plbot, event), остальным — (cardinal, event)."""
        for info in list(self.plugins.values()):
            if not self.enabled(info.uuid):
                continue
            first = self.app.pl_compat if info.kind == "playerok-universal" else self.app.cardinal
            for fn in info.hooks.get(hook) or []:
                try:
                    fn(first, ev)
                except Exception as e:
                    log.error("🧩 %s → %s: %s", info.name, hook, e)

    def delete(self, uid) -> bool:
        info = self.plugins.get(uid)
        if not info:
            return False
        self.call("BIND_TO_DELETE", self.app.cardinal, uid=uid)
        if info.path.is_dir():
            shutil.rmtree(info.path, ignore_errors=True)
        else:
            info.path.unlink(missing_ok=True)
        self.plugins.pop(uid, None)
        return True

    def reload(self, uid) -> Optional[PluginInfo]:
        info = self.plugins.pop(uid, None)
        if not info:
            return None
        path = info.path / "__init__.py" if info.path.is_dir() else info.path
        return self.load(path, folder=info.path if info.path.is_dir() else None)

    def install_from_url(self, url: str) -> Optional[PluginInfo]:
        url = url.strip()
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
        if m:
            url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        name = url.split("?")[0].rstrip("/").split("/")[-1] or "plugin.py"
        dst = self.save_upload(name, r.content)
        return self.load(dst)

    def save_upload(self, filename: str, content: bytes) -> Path:
        filename = re.sub(r"[^\w.\-]", "_", Path(filename).name)
        if not filename.endswith(".py"):
            raise ValueError("Нужен файл .py")
        compile(content, filename, "exec")  # проверка синтаксиса
        dst = PLUGINS_DIR / filename
        dst.write_bytes(content)
        return dst


