"""Совместимость с FunPay Cardinal и Playerok Universal: заглушки модулей, события, объект cardinal."""
from __future__ import annotations

import configparser
import time
import types
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App

from . import VERSION

class _Anything:
    """Заглушка: любой атрибут/вызов/наследование — без падений."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]  # работает как декоратор
        return _Anything()

    def __getattr__(self, n):
        if n.startswith("__") and n.endswith("__"):
            raise AttributeError(n)
        return _Anything()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __str__(self):
        return ""

    def __mro_entries__(self, bases):
        return (object,)

    def __getitem__(self, k):
        return _Anything()

    def __eq__(self, o):
        return isinstance(o, _Anything)

    def __hash__(self):
        return 0


class _StubModule(types.ModuleType):
    def __init__(self, name, attrs=None):
        super().__init__(name)
        self.__dict__.update(attrs or {})
        self.__path__ = []  # пакет

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Anything()


# --- события в стиле FunPayAPI.updater.events ---
class EventTypes(Enum):
    INITIAL_CHAT = 0
    CHATS_LIST_CHANGED = 1
    LAST_CHAT_MESSAGE_CHANGED = 2
    NEW_MESSAGE = 3
    INITIAL_ORDER = 4
    ORDERS_LIST_CHANGED = 5
    NEW_ORDER = 6
    ORDER_STATUS_CHANGED = 7


class _BaseEvent:
    def __init__(self, runner_tag="", etype=None):
        self.runner_tag, self.type = runner_tag, etype
        self.time = time.time()


class NewMessageEvent(_BaseEvent):
    def __init__(self, runner_tag, message, stack=None):
        super().__init__(runner_tag, EventTypes.NEW_MESSAGE)
        self.message, self.stack = message, stack


class NewOrderEvent(_BaseEvent):
    def __init__(self, runner_tag, order):
        super().__init__(runner_tag, EventTypes.NEW_ORDER)
        self.order = order


class OrderStatusChangedEvent(_BaseEvent):
    def __init__(self, runner_tag, order):
        super().__init__(runner_tag, EventTypes.ORDER_STATUS_CHANGED)
        self.order = order


class LastChatMessageChangedEvent(_BaseEvent):
    def __init__(self, runner_tag, chat):
        super().__init__(runner_tag, EventTypes.LAST_CHAT_MESSAGE_CHANGED)
        self.chat = chat


class CBT:
    """Префиксы callback'ов Cardinal (для кнопок плагинов)."""
    MAIN = "1"
    MAIN2 = "2"
    MAIN3 = "3"
    PLUGINS_LIST = "44"
    EDIT_PLUGIN = "45"
    PLUGIN_SETTINGS = "PlSet"
    PLUGIN_COMMANDS = "PlCom"
    TOGGLE_PLUGIN = "PlTog"
    DELETE_PLUGIN = "PlDel"
    BACK = "BACK"
    CLEAR_STATE = "CLEAR_STATE"
    EMPTY = "EMPTY"
    UPDATE_PROFILE = "UPDATE_PROFILE"


class StubLocalizer:
    def __init__(self, *a, **k):
        pass

    def translate(self, key, *args, language=None):
        return str(key)


class _CfgSection(dict):
    def getboolean(self, k, fallback=False):
        v = self.get(k, fallback)
        return str(v).lower() in ("1", "true", "yes", "on")


class CardinalCompat:
    """Объект, который получают плагины вместо Cardinal."""

    def __init__(self, app: "App"):
        self.duo = app
        self.castor = app
        self.app = app
        self.VERSION = VERSION
        self.running = False
        self.run_id = 0
        self.blacklist = app.cfg.get("blacklist.users", [])
        self.old_users = {}
        self.MAIN_CFG = configparser.ConfigParser()
        self.AR_CFG = configparser.ConfigParser()
        self.AD_CFG = configparser.ConfigParser()
        self.RAW_AR_CFG = self.AR_CFG
        self._fill_cfgs()
        self.telegram_commands: dict = {}
        self.balance = None
        self.delivery_tests = {}

    def _fill_cfgs(self):
        c = self.duo.cfg
        self.MAIN_CFG.read_dict({
            "FunPay": {"golden_key": "", "user_agent": c.get("funpay.user_agent", ""),
                       "autoRaise": str(int(c.get("funpay.auto_raise.enabled", False))),
                       "autoResponse": str(int(c.get("funpay.auto_reply", True))),
                       "autoDelivery": str(int(c.get("funpay.auto_delivery", True))),
                       "locale": "ru", "oldMsgGetMode": "0", "keepSentMessagesUnread": "0"},
            "Telegram": {"enabled": str(int(c.get("telegram.enabled", True))), "token": "", "blockLogin": "0"},
            "Greetings": {"sendGreetings": str(int(c.get("funpay.greeting.enabled", True))),
                          "greetingsText": c.get("funpay.greeting.text", "")},
            "Other": {"watermark": c.get("general.watermark", ""), "language": "ru"},
            "Proxy": {"enable": "0", "ip": "", "port": "", "login": "", "password": "", "check": "0"},
        })
        for cmd in c.get("commands", []):
            try:
                self.AR_CFG[cmd["trigger"]] = {"response": cmd.get("text", ""), "telegramNotification": str(int(cmd.get("notify", False)))}
            except Exception:
                pass

    @property
    def account(self):
        svc = self.duo.service("funpay")
        return svc.client if svc else None

    @property
    def telegram(self):
        return self.duo.tg if (self.duo.tg and self.duo.tg.bot) else None

    @property
    def playerok(self):
        svc = self.duo.service("playerok")
        return svc.bridge if svc else None

    @property
    def plugins(self):
        return {u: types.SimpleNamespace(name=i.name, enabled=self.duo.plugins.enabled(u), uuid=u,
                                         settings_page=i.settings_page, path=str(i.path),
                                         plugin=i.module, description=i.description, version=i.version,
                                         credits=i.credits, commands=i.commands, delete_handler=None)
                for u, i in self.duo.plugins.plugins.items()}

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True, **kw):
        return self.duo.send("funpay", chat_id, message_text, chat_name=chat_name)

    def add_telegram_commands(self, uuid_, commands: list):
        info = self.duo.plugins.plugins.get(uuid_)
        for c in commands:
            if info is not None:
                info.commands.append({"cmd": c[0], "desc": c[1]})
            self.telegram_commands[c[0]] = c[1]
        if self.duo.tg:
            self.duo.tg.refresh_commands()

    def save_config(self, *a, **k):
        pass

    def get_balance(self, *a, **k):
        return self.account.balance if self.account else None

    def update_session(self, *a, **k):
        return self.account.login() if self.account else None

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Anything()


class PlayerokCompatBot:
    """Передаётся обработчикам модулей Playerok Universal."""

    def __init__(self, app: "App" = None):
        self.app = app

    @property
    def account(self):
        svc = self.app.service("playerok") if self.app else None
        return svc.bridge.acc if svc else None

    @property
    def playerok_account(self):
        return self.account

    def send_message(self, chat_id, text, *a, **k):
        return self.app.send("playerok", chat_id, text)

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Anything()


