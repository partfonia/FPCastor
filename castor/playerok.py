"""Playerok: мост к библиотеке playerokapi (alleexxeeyy/PlayerokAPI)."""
from __future__ import annotations

import importlib
import inspect
import re

from .bootstrap import playerok_status
from .core import DEFAULT_UA, Config, _g


class PlayerokBridge:
    def __init__(self, cfg: Config, acc: dict = None):
        self.cfg = cfg
        self.acc_cfg = acc or {}
        self.acc = None
        self.id = None
        self.username = None
        self.ok = False
        self.lib = None

    def connect(self):
        ok, why = playerok_status()
        if not ok:
            raise RuntimeError(why)
        from playerokapi.account import Account  # type: ignore
        self.lib = importlib.import_module("playerokapi")
        params = inspect.signature(Account.__init__).parameters
        a = self.acc_cfg
        token = (a.get("token") or self.cfg.get("playerok.token", "")).strip()
        cookies = (a.get("cookies") or self.cfg.get("playerok.cookies", "")).strip() or (f"token={token}" if token else "")
        if not token and cookies:
            m = re.search(r"token=([^;]+)", cookies)
            token = m.group(1) if m else ""
        kw = {}
        if "token" in params:
            kw["token"] = token
        if "cookies" in params and cookies:
            kw["cookies"] = cookies
        if "user_agent" in params:
            kw["user_agent"] = a.get("user_agent") or self.cfg.get("playerok.user_agent") or DEFAULT_UA
        proxy = (a.get("proxy") or self.cfg.get("playerok.proxy", "")).strip()
        if "proxy" in params and proxy:
            kw["proxy"] = proxy
        acc = Account(**kw)
        self.acc = acc.get() if hasattr(acc, "get") else acc
        self.id = getattr(self.acc, "id", None)
        self.username = getattr(self.acc, "username", None)
        self.ok = True
        return self

    def send_message(self, chat_id, text):
        return self.acc.send_message(chat_id=chat_id, text=text)

    def mark_sent(self, deal_id):
        from playerokapi.enums import ItemDealStatuses  # type: ignore
        return self.acc.update_deal(deal_id, ItemDealStatuses.SENT)

    def listen(self):
        from playerokapi.listener.listener import EventListener  # type: ignore
        for ev in EventListener(self.acc).listen():
            yield ev

    def balance(self):
        for path in (("profile", "balance", "value"), ("profile", "balance"), ("balance", "value"), ("balance",)):
            v = _g(self.acc, *path)
            if isinstance(v, (int, float, str)):
                return v
        return None

    # ---- чаты (по возможностям установленной версии библиотеки) ----
    def get_chats(self, count=24) -> list:
        res = self.acc.get_chats(count=count) if "count" in inspect.signature(self.acc.get_chats).parameters \
            else self.acc.get_chats()
        chats = _g(res, "chats", default=res) or []
        out = []
        for ch in chats:
            users = _g(ch, "users", default=[]) or []
            other = next((u for u in users if str(_g(u, "id")) != str(self.id)), None)
            out.append({"id": str(_g(ch, "id")), "name": _g(other, "username", default="") or "",
                        "last_text": _g(ch, "last_message", "text", default="") or "",
                        "unread": bool(_g(ch, "unread_messages_counter", default=0))})
        return out

    def get_chat_messages(self, chat_id, count=30) -> list:
        fn = self.acc.get_chat_messages
        res = fn(chat_id, count) if len(inspect.signature(fn).parameters) >= 2 else fn(chat_id)
        msgs = _g(res, "messages", default=res) or []
        out = []
        for m in msgs:
            uid = _g(m, "user", "id")
            out.append({"id": str(_g(m, "id")), "author": _g(m, "user", "username", default="Playerok"),
                        "by_me": uid is not None and str(uid) == str(self.id),
                        "text": (_g(m, "text", default="") or "") + (" " + _g(m, "file", "url") if _g(m, "file", "url") else "")})
        return list(reversed(out))

    # ---- вывод средств (по возможностям установленной playerokapi) ----
    def withdraw_providers(self) -> list:
        try:
            enums = importlib.import_module("playerokapi.enums")
        except Exception:
            return []
        for name in ("TransactionProviderIds", "TransactionProviders", "WithdrawalProviders"):
            e = getattr(enums, name, None)
            if e is not None:
                return [m.name for m in e]
        return []

    def withdraw(self, provider: str, wallet: str, amount: float, sbp_bank: str = ""):
        fn = getattr(self.acc, "request_withdrawal", None)
        if fn is None:
            raise RuntimeError("Установленная версия playerokapi не умеет выводить средства")
        params = inspect.signature(fn).parameters
        enums = importlib.import_module("playerokapi.enums")
        prov = provider
        for name in ("TransactionProviderIds", "TransactionProviders", "WithdrawalProviders"):
            e = getattr(enums, name, None)
            if e is not None and provider in e.__members__:
                prov = e[provider]
                break
        values = {"provider": prov, "account": wallet, "value": int(amount), "amount": int(amount),
                  "sbp_bank_member_id": sbp_bank or None, "payment_method_id": None}
        kw = {k: v for k, v in values.items() if k in params and v is not None}
        missing = [k for k, prm in params.items() if k != "self" and prm.default is inspect.Parameter.empty
                   and k not in kw]
        if missing:
            raise RuntimeError(f"playerokapi ждёт параметры {missing}, которые бот не знает — вывод не выполнен")
        return fn(**kw)
