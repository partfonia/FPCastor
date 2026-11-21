"""Пути, логи, конфиг, хранилища, общие модели и утилиты."""
from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import string
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests

from . import VERSION, APP_NAME  # noqa: F401

# ════════════════════════ ПУТИ ════════════════════════
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "castor_data"
for _old in ("gemini_data", "duo_data"):  # перенос данных прошлых версий (Gemini, DuoSeller)
    if not DATA_DIR.exists() and (BASE_DIR / _old).exists():
        (BASE_DIR / _old).rename(DATA_DIR)
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
STATS_PATH = DATA_DIR / "stats.json"
CUSTOMERS_PATH = DATA_DIR / "customers.json"
PRODUCTS_DIR = DATA_DIR / "products"
LOGS_DIR = DATA_DIR / "logs"
BIN_DIR = DATA_DIR / "bin"
BACKUP_DIR = DATA_DIR / "backups"
PLUGINS_DIR = BASE_DIR / "plugins"
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"

for _d in (DATA_DIR, PRODUCTS_DIR, LOGS_DIR, BIN_DIR, BACKUP_DIR, PLUGINS_DIR,
           BASE_DIR / "storage" / "plugins", BASE_DIR / "storage" / "cache", BASE_DIR / "configs"):
    _d.mkdir(parents=True, exist_ok=True)

# ════════════════════════ ЛОГИ ════════════════════════
LOG_BUFFER: deque = deque(maxlen=600)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


def setup_logging(level="INFO"):
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%d.%m %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    fh = RotatingFileHandler(LOGS_DIR / "fpcastor.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8")
    bh = _BufferHandler()
    for h in (sh, fh, bh):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("urllib3", "TeleBot", "telebot", "httpx", "websockets", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("duo")

# ════════════════════════ КОНФИГ ════════════════════════
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_CONFIG: dict = {
    "general": {
        "log_level": "INFO",
        "work_hours": {"enabled": False, "from": "09:00", "to": "23:00",
                       "away_text": "Здравствуйте, $username! Сейчас нерабочее время, отвечу утром. "
                                    "Автовыдача работает круглосуточно."},
        # подпись перед сообщениями бота в чате покупателя (как у FunPay Cardinal)
        "watermark": "♊ 𝑪𝒂𝒔𝒕𝒐𝒓",
        "watermark_enabled": True,
        "watermark_newline": True,     # подпись отдельной строкой
        "watermark_manual": True,      # и в ответах, отправленных из Telegram / мини-аппа
        "send_delay_sec": 0.5,
    },
    "project": {
        "links": {"updates": "@fpcastor_updates", "plugins": "@fpcastor_plugins", "chat": "@fpcastor_chat",
                  "donate": "@fpcastor_donate", "github": "github.com/partfonia/FPCastor"},
        "set_bot_description": True,   # бот сам заполняет «Что умеет этот бот?» и «О себе»
    },
    "telegram": {
        "enabled": True,
        "token": "",
        "password": "",
        "admins": [],               # устарело: переносится в users
        "users": [],                # [{"id": 123, "role": "owner|admin|manager", "name": ""}]
        "extra_chats": [],          # группы/каналы для уведомлений (id)
        "proxy": {"enabled": False, "list": [], "check_on_start": True, "failover": True,
                  "allow_direct": True},
        "notify": {
            "fp_message": True, "fp_order": True, "fp_confirm": True, "fp_review": True,
            "pl_message": True, "pl_deal": True, "pl_confirm": True, "pl_review": True,
            "delivery": True, "low_stock": True, "raise": False, "errors": True, "start": True,
            "reminder": False, "report": True, "stock_out": True, "supplier": True, "withdraw": True,
            "finance": True,
        },
    },
    "web": {"enabled": True, "host": "0.0.0.0", "port": 8765, "public_url": "",
            "tunnel": "none", "password": ""},
    # Аккаунты площадок. Поведение (тексты, автоответы) — в разделах funpay / playerok, ниже.
    "accounts": {
        "funpay": [],    # {"id": "fp1", "name": "", "enabled": True, "golden_key": "", "proxy": "", "user_agent": "",
                         #  "auto_raise": True, "auto_delivery": True}
        "playerok": [],  # {"id": "pl1", "name": "", "enabled": True, "token": "", "cookies": "", "proxy": "",
                         #  "user_agent": "", "auto_delivery": True}
    },
    "funpay": {
        "enabled": True,
        "golden_key": "",            # устарело: переносится в accounts.funpay
        "user_agent": DEFAULT_UA,
        "proxy": "",
        "poll_interval": 6,
        "auto_raise": {"enabled": True, "check_every_min": 10},
        "greeting": {"enabled": True, "cooldown_days": 7,
                     "text": "Привет, $username! 👋 Скоро отвечу. Команды: !помощь"},
        "auto_reply": True,
        "auto_delivery": True,
        "auto_refund_no_stock": False,
        "confirm_reply": {"enabled": True,
                          "text": "Спасибо за подтверждение заказа #$order_id, $username! Буду рад отзыву ⭐"},
        "review_reply": {"enabled": False, "texts": {
            "5": "Спасибо за отличный отзыв, $username! ❤️", "4": "Спасибо за отзыв!",
            "3": "Спасибо, учту замечания.", "2": "Жаль, что не понравилось. Напишите — решим.",
            "1": "Напишите мне в чат — разберёмся."}},
        "order_text": {"enabled": False, "text": "Спасибо за покупку «$order_title»! Скоро выдам товар."},
    },
    "playerok": {
        "enabled": False,
        "token": "",                 # устарело: переносится в accounts.playerok
        "cookies": "",
        "user_agent": DEFAULT_UA,
        "proxy": "",
        "greeting": {"enabled": True, "cooldown_days": 7,
                     "text": "Привет, $username! 👋 Скоро отвечу. Команды: !помощь"},
        "auto_reply": True,
        "auto_delivery": True,
        "mark_sent": True,
        "confirm_reply": {"enabled": True, "text": "Спасибо за подтверждение, $username! Оставьте отзыв ⭐"},
        "order_text": {"enabled": False, "text": "Спасибо за покупку «$order_title»!"},
    },
    "features": {
        "returning_buyer": {"enabled": True,
                            "text": "С возвращением, $username! Это ваш $orders_count-й заказ, спасибо 🙌"},
        "confirm_reminder": {"enabled": False, "after_hours": 24,
                             "text": "$username, если всё получили — подтвердите, пожалуйста, заказ «$order_title» 🙏"},
        "daily_report": {"enabled": True, "time": "23:55"},
        "low_stock": {"enabled": True, "threshold": 3},
        "auto_backup": {"enabled": True, "keep": 7},
        "anti_flood": {"enabled": True, "max_replies_per_min": 4},
        "vacation": {"enabled": False, "disable_delivery": False,
                     "text": "$username, я в отпуске до $vacation_until. Автовыдача работает, на вопросы отвечу позже.",
                     "until": ""},
        "webhooks": {"enabled": False, "urls": [], "events": ["order", "message", "delivery"]},
        # Лоты без товара: off — ничего, notify — только сообщить, ask — спросить «скрыть?», auto — скрыть сразу
        "out_of_stock": {"mode": "ask", "restore": "ask"},
    },
    "finance": {
        # % комиссии площадки, если сумма заказа в боте больше, чем приходит вам на баланс
        "commission": {"funpay": 0.0, "playerok": 0.0},
        "expenses": [],   # [{"id", "date": "2026-11-21", "amount": 100, "note": "", "platform": ""}]
    },
    # API поставщиков для автовыдачи (режим правила «supplier»)
    "suppliers": [],
    # Автовывод: mode off | ask (спросить владельца) | auto
    "withdraw": {
        "funpay": {"mode": "off", "currency": "rub", "method": "card_rub", "wallet": "",
                   "threshold": 5000, "keep": 0, "min_amount": 1000, "max_per_day": 50000,
                   "check_every_hours": 6, "account": ""},
        "playerok": {"mode": "off", "provider": "", "wallet": "", "sbp_bank": "",
                     "threshold": 5000, "keep": 0, "min_amount": 1000, "max_per_day": 50000,
                     "check_every_hours": 6, "account": ""},
    },
    "commands": [
        {"trigger": "!помощь", "match": "exact", "platforms": ["funpay", "playerok"], "notify": False,
         "text": "Команды:\n!продавец — позвать продавца\n!помощь — это сообщение"},
        {"trigger": "!продавец", "match": "exact", "platforms": ["funpay", "playerok"], "notify": True,
         "text": "Я позвал продавца, он скоро ответит 🙌"},
    ],
    "delivery": [],
    "blacklist": {"users": [], "block_delivery": True, "block_reply": True},
    "templates": ["Здравствуйте! Уже смотрю.", "Секунду, проверяю заказ.", "Спасибо за покупку!"],
    "plugins": {"disabled": []},
}

SECRET_KEYS = {"golden_key", "token", "cookies", "password", "headers", "wallet", "api_key"}
_cfg_lock = threading.RLock()


def deep_merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Не удалось прочитать %s: %s", path, e)
    return json.loads(json.dumps(default))


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Config:
    def __init__(self):
        self.data = deep_merge(DEFAULT_CONFIG, load_json(CONFIG_PATH, {}))
        self.listeners: list = []   # функции (old, new) после сохранения
        if self.migrate() and CONFIG_PATH.exists():
            self.save()

    def migrate(self) -> bool:
        """Переносит настройки старых версий: один аккаунт → список, admins → users с ролями."""
        d, changed = self.data, False
        acc = d.setdefault("accounts", {"funpay": [], "playerok": []})
        if not acc.get("funpay") and d["funpay"].get("golden_key"):
            acc["funpay"] = [{"id": "fp1", "name": "Основной", "enabled": True,
                              "golden_key": d["funpay"]["golden_key"], "proxy": d["funpay"].get("proxy", ""),
                              "user_agent": d["funpay"].get("user_agent", DEFAULT_UA),
                              "auto_raise": d["funpay"].get("auto_raise", {}).get("enabled", True), "auto_delivery": True}]
            d["funpay"]["golden_key"] = ""
            changed = True
        if not acc.get("playerok") and (d["playerok"].get("token") or d["playerok"].get("cookies")):
            acc["playerok"] = [{"id": "pl1", "name": "Основной", "enabled": True,
                                "token": d["playerok"]["token"], "cookies": d["playerok"].get("cookies", ""),
                                "proxy": d["playerok"].get("proxy", ""),
                                "user_agent": d["playerok"].get("user_agent", DEFAULT_UA), "auto_delivery": True}]
            d["playerok"]["token"] = d["playerok"]["cookies"] = ""
            changed = True
        tg = d["telegram"]
        if tg.get("admins") and not tg.get("users"):
            tg["users"] = [{"id": int(a), "role": "owner", "name": ""} for a in tg["admins"]]
            tg["admins"] = []
            changed = True
        for r in d.get("delivery", []):
            r.setdefault("cost", 0)
            r.setdefault("lots", [])
        # переименование Gemini → Castor: подпись и ссылки, если их не меняли вручную
        g = d["general"]
        old_wm = {"♊ 𝑮𝒆𝒎𝒊𝒏𝒊": "♊ 𝑪𝒂𝒔𝒕𝒐𝒓", "𝑮𝒆𝒎𝒊𝒏𝒊 ♊": "𝑪𝒂𝒔𝒕𝒐𝒓 ♊", "✦ 𝓖𝓮𝓶𝓲𝓷𝓲 ✦": "✦ 𝓒𝓪𝓼𝓽𝓸𝓻 ✦",
                  "♊︎ 𝙂𝙚𝙢𝙞𝙣𝙞": "♊︎ 𝘾𝙖𝙨𝙩𝙤𝙧"}
        if g.get("watermark") in old_wm:
            g["watermark"] = old_wm[g["watermark"]]
            changed = True
        for k, v in list(d.get("project", {}).get("links", {}).items()):
            if isinstance(v, str) and v.startswith("@fpgemini_"):
                d["project"]["links"][k] = v.replace("@fpgemini_", "@fpcastor_")
                changed = True
        return changed

    def accounts(self, platform) -> list:
        return list(self.get(f"accounts.{platform}", []))

    def account(self, acc_id) -> Optional[dict]:
        for p in ("funpay", "playerok"):
            for a in self.accounts(p):
                if a.get("id") == acc_id:
                    return a
        return None

    def new_account_id(self, platform) -> str:
        prefix = "fp" if platform == "funpay" else "pl"
        used = {a.get("id") for a in self.accounts(platform)}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    @property
    def exists(self):
        return CONFIG_PATH.exists()

    def save(self):
        with _cfg_lock:
            save_json(CONFIG_PATH, self.data)

    def get(self, path: str, default=None):
        cur = self.data
        for p in path.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def set(self, path: str, value, save=True):
        with _cfg_lock:
            parts = path.split(".")
            cur = self.data
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
            if save:
                self.save()

    def toggle(self, path: str) -> bool:
        v = not bool(self.get(path))
        self.set(path, v)
        return v

    def masked(self) -> dict:
        def mask(d):
            if isinstance(d, dict):
                return {k: ("***" if k in SECRET_KEYS and isinstance(v, str) and v else mask(v)) for k, v in d.items()}
            if isinstance(d, list):
                return [mask(x) for x in d]
            return d
        return mask(self.data)

    def update_from_masked(self, new: dict, protect: tuple = ()):
        """protect — разделы, которые пользователь без прав менять не может (остаются как были).
        "secrets" в protect — все секретные поля сохраняют старые значения."""
        keep_secrets = "secrets" in protect

        def unmask(new_d, old_d):
            if isinstance(new_d, dict):
                res = {}
                for k, v in new_d.items():
                    old_v = old_d.get(k) if isinstance(old_d, dict) else None
                    if k in SECRET_KEYS and isinstance(v, str) and (v == "***" or keep_secrets):
                        res[k] = old_v if isinstance(old_v, str) else ("" if keep_secrets else v)
                    else:
                        res[k] = unmask(v, old_v)
                return res
            if isinstance(new_d, list) and isinstance(old_d, list):
                return [unmask(x, _match(x, old_d, i)) for i, x in enumerate(new_d)]
            return new_d

        def _match(item, old_list, i):
            # элементы списков (аккаунты, поставщики) сопоставляем по id, иначе по позиции
            if isinstance(item, dict) and item.get("id"):
                for o in old_list:
                    if isinstance(o, dict) and o.get("id") == item["id"]:
                        return o
                return None
            return old_list[i] if i < len(old_list) else None
        with _cfg_lock:
            old = json.loads(json.dumps(self.data))
            merged = deep_merge(DEFAULT_CONFIG, unmask(new, self.data))
            for path in protect:
                if path == "secrets":
                    continue
                cur_old, cur_new = old, merged
                parts = path.split(".")
                for p_ in parts[:-1]:
                    cur_old, cur_new = cur_old.get(p_, {}), cur_new.setdefault(p_, {})
                if parts[-1] in cur_old:
                    cur_new[parts[-1]] = cur_old[parts[-1]]
            self.data = merged
            self.save()
        for fn in self.listeners:
            try:
                fn(old, self.data)
            except Exception as e:
                log.error("config listener: %s", e)


# ════════════════════════ ХРАНИЛИЩА ════════════════════════
class State:
    """Приветствия, ответы на отзывы, открытые заказы, напоминания."""
    BUCKETS = ("greeted", "away", "reviewed", "confirmed", "open_orders", "reminded", "flags", "invites",
               "hidden_lots", "supplier_orders", "withdraw_log", "pending")

    def __init__(self):
        self.lock = threading.RLock()
        self.data = load_json(STATE_PATH, {})
        for k in self.BUCKETS:
            self.data.setdefault(k, {})

    def save(self):
        with self.lock:
            save_json(STATE_PATH, self.data)

    def mark(self, bucket, key, value=None):
        with self.lock:
            self.data.setdefault(bucket, {})[str(key)] = value if value is not None else time.time()
            self.save()

    def get(self, bucket, key, default=None):
        return self.data.get(bucket, {}).get(str(key), default)

    def pop(self, bucket, key):
        with self.lock:
            v = self.data.get(bucket, {}).pop(str(key), None)
            self.save()
            return v


class Stats:
    """Заказы и деньги. Прибыль = сумма − комиссия площадки − себестоимость; минус расходы за период."""

    def __init__(self, cfg: "Config" = None):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.data = load_json(STATS_PATH, {"orders": [], "messages": {"funpay": 0, "playerok": 0},
                                           "deliveries": 0, "raises": 0, "refunds": 0})
        self.started = time.time()

    def save(self):
        with self.lock:
            self.data["orders"] = self.data["orders"][-20000:]
            save_json(STATS_PATH, self.data)

    def commission_pct(self, platform) -> float:
        try:
            return float(self.cfg.get(f"finance.commission.{platform}", 0) or 0) if self.cfg else 0.0
        except (TypeError, ValueError):
            return 0.0

    def add_order(self, o: "UOrder"):
        with self.lock:
            price = float(o.price or 0)
            self.data["orders"].append({
                "p": o.platform, "a": o.account, "id": str(o.id), "t": time.time(), "price": price,
                "title": o.title[:120], "buyer": o.buyer, "amount": o.amount, "delivered": False,
                "cost": None, "commission": round(price * self.commission_pct(o.platform) / 100, 2),
                "refunded": False})
            self.save()

    def find(self, oid, platform=None) -> Optional[dict]:
        for o in reversed(self.data["orders"]):
            if o["id"] == str(oid) and (not platform or o["p"] == platform):
                return o
        return None

    def set_order_flag(self, oid, key, value, platform=None):
        with self.lock:
            o = self.find(oid, platform)
            if o is not None:
                o[key] = value
                self.save()
            return o

    def add_cost(self, oid, cost, platform=None):
        """Себестоимость накапливается (выдача частями, докупка у поставщика)."""
        with self.lock:
            o = self.find(oid, platform)
            if o is not None:
                o["cost"] = round(float(o.get("cost") or 0) + float(cost or 0), 2)
                self.save()

    def inc(self, key, sub=None):
        with self.lock:
            if sub:
                self.data.setdefault(key, {}).setdefault(sub, 0)
                self.data[key][sub] += 1
            else:
                self.data[key] = self.data.get(key, 0) + 1
            self.save()

    @staticmethod
    def profit_of(o) -> float:
        if o.get("refunded"):
            return 0.0
        return round(o["price"] - float(o.get("commission") or 0) - float(o.get("cost") or 0), 2)

    def _agg(self, orders) -> dict:
        live = [o for o in orders if not o.get("refunded")]
        rev = round(sum(o["price"] for o in live), 2)
        com = round(sum(float(o.get("commission") or 0) for o in live), 2)
        cost = round(sum(float(o.get("cost") or 0) for o in live), 2)
        return {"count": len(live), "sum": rev, "commission": com, "cost": cost,
                "profit": round(rev - com - cost, 2), "refunded": len(orders) - len(live),
                "no_cost": sum(1 for o in live if o.get("cost") is None)}

    def expenses_between(self, a: float, b: float) -> float:
        total = 0.0
        for e in (self.cfg.get("finance.expenses", []) if self.cfg else []):
            try:
                t = datetime.strptime(e.get("date", ""), "%Y-%m-%d").timestamp()
            except ValueError:
                continue
            if a <= t < b:
                total += float(e.get("amount") or 0)
        return round(total, 2)

    def period(self, sec, account=None) -> dict:
        now = time.time()
        row = {}
        for p in ("funpay", "playerok"):
            row[p] = self._agg([o for o in self.data["orders"] if o["p"] == p and now - o["t"] < sec
                                and (not account or o.get("a") == account)])
        exp = self.expenses_between(now - sec, now + 1)
        row["total"] = {"sum": round(row["funpay"]["sum"] + row["playerok"]["sum"], 2),
                        "profit": round(row["funpay"]["profit"] + row["playerok"]["profit"] - exp, 2),
                        "expenses": exp,
                        "count": row["funpay"]["count"] + row["playerok"]["count"]}
        return row

    def today(self) -> dict:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.period(time.time() - start + 1)

    def daily(self, days=14) -> list:
        res = []
        today = datetime.now().date()
        for i in range(days - 1, -1, -1):
            day = today - timedelta(days=i)
            a = datetime.combine(day, datetime.min.time()).timestamp()
            b = a + 86400
            row = {"date": day.strftime("%d.%m"), "funpay": 0.0, "playerok": 0.0, "count": 0, "profit": 0.0}
            for o in self.data["orders"]:
                if a <= o["t"] < b and not o.get("refunded"):
                    row[o["p"]] = round(row[o["p"]] + o["price"], 2)
                    row["profit"] = round(row["profit"] + self.profit_of(o), 2)
                    row["count"] += 1
            row["profit"] = round(row["profit"] - self.expenses_between(a, b), 2)
            res.append(row)
        return res

    def top_products(self, n=10) -> list:
        agg: dict = {}
        for o in self.data["orders"]:
            if o.get("refunded"):
                continue
            k = o["title"][:60]
            a = agg.setdefault(k, {"title": k, "count": 0, "sum": 0.0, "profit": 0.0})
            a["count"] += 1
            a["sum"] = round(a["sum"] + o["price"], 2)
            a["profit"] = round(a["profit"] + self.profit_of(o), 2)
        return sorted(agg.values(), key=lambda x: -x["profit"])[:n]

    def by_account(self, sec=2592000) -> dict:
        now = time.time()
        out: dict = {}
        for o in self.data["orders"]:
            if now - o["t"] < sec and not o.get("refunded"):
                a = out.setdefault(o.get("a") or "?", {"count": 0, "sum": 0.0, "profit": 0.0})
                a["count"] += 1
                a["sum"] = round(a["sum"] + o["price"], 2)
                a["profit"] = round(a["profit"] + self.profit_of(o), 2)
        return out

    def summary(self) -> dict:
        res = {name: self.period(sec) for name, sec in
               (("day", 86400), ("week", 604800), ("month", 2592000), ("all", 10 ** 12))}
        res["today"] = self.today()
        res["messages"] = self.data.get("messages", {})
        res["deliveries"] = self.data.get("deliveries", 0)
        res["raises"] = self.data.get("raises", 0)
        res["refunds"] = self.data.get("refunds", 0)
        res["uptime"] = int(time.time() - self.started)
        res["last_orders"] = [dict(o, profit=self.profit_of(o)) for o in self.data["orders"][-30:][::-1]]
        res["daily"] = self.daily()
        res["top"] = self.top_products()
        res["by_account"] = self.by_account()
        return res

    def search(self, q="", platform="", limit=200, account="") -> list:
        q = (q or "").lower()
        out = []
        for o in reversed(self.data["orders"]):
            if platform and o["p"] != platform or account and o.get("a") != account:
                continue
            if q and q not in (o["title"] + " " + o["buyer"] + " " + o["id"]).lower():
                continue
            out.append(dict(o, profit=self.profit_of(o)))
            if len(out) >= limit:
                break
        return out


def strip_finance(obj):
    """Убирает денежные поля для пользователей без права finance."""
    money = {"price", "sum", "profit", "cost", "commission", "balance", "spent", "expenses"}
    if isinstance(obj, dict):
        return {k: (None if k in money else strip_finance(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_finance(x) for x in obj]
    return obj


class Customers:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = load_json(CUSTOMERS_PATH, {})

    def save(self):
        with self.lock:
            save_json(CUSTOMERS_PATH, self.data)

    def key(self, platform, name):
        return f"{platform}:{name}"

    def add_order(self, o: "UOrder") -> dict:
        with self.lock:
            c = self.data.setdefault(self.key(o.platform, o.buyer), {
                "platform": o.platform, "name": o.buyer, "id": o.buyer_id, "orders": 0, "spent": 0.0,
                "first": time.time(), "last": 0, "note": "", "chat_id": None})
            c["orders"] += 1
            c["spent"] = round(c["spent"] + float(o.price or 0), 2)
            c["last"] = time.time()
            c["chat_id"] = str(o.chat_id) if o.chat_id is not None else c.get("chat_id")
            self.save()
            return c

    def get(self, platform, name):
        return self.data.get(self.key(platform, name))

    def set_note(self, platform, name, note):
        with self.lock:
            c = self.data.setdefault(self.key(platform, name), {"platform": platform, "name": name, "orders": 0,
                                                                 "spent": 0.0, "first": time.time(), "last": 0})
            c["note"] = note
            self.save()

    def list(self, q="", limit=300):
        q = (q or "").lower()
        items = [c for c in self.data.values() if not q or q in c["name"].lower() or q in (c.get("note") or "").lower()]
        return sorted(items, key=lambda c: -c.get("spent", 0))[:limit]


class Products:
    lock = threading.Lock()
    on_change = None   # callback(name, old_count, new_count) — для лотов без товара

    @staticmethod
    def safe_name(name: str) -> str:
        name = re.sub(r"[^\w.\-а-яА-ЯёЁ ]", "_", Path(str(name)).name).strip() or "products.txt"
        return name if name.endswith(".txt") else name + ".txt"

    @classmethod
    def _path(cls, name: str) -> Path:
        return PRODUCTS_DIR / cls.safe_name(name)

    @classmethod
    def _lines(cls, p: Path) -> list:
        return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    @classmethod
    def count(cls, name) -> int:
        return len(cls._lines(cls._path(name)))

    @classmethod
    def take(cls, name, n=1) -> Optional[list]:
        with cls.lock:
            p = cls._path(name)
            lines = cls._lines(p)
            if len(lines) < n:
                return None
            got, rest = lines[:n], lines[n:]
            p.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
            cls._changed(p.name, len(lines), len(rest))
            with open(LOGS_DIR / "delivered.log", "a", encoding="utf-8") as f:
                for g in got:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{name}\t{g}\n")
            return got

    @classmethod
    def add(cls, name, lines: list, replace=False, front=False):
        with cls.lock:
            p = cls._path(name)
            old = [] if replace else cls._lines(p)
            add = [x.strip() for x in lines if x.strip()]
            before = len(cls._lines(p))
            new = add + old if front else old + add
            p.write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
        cls._changed(p.name, before, len(new))
        return len(new)

    @classmethod
    def _changed(cls, name, old, new):
        if cls.on_change and old != new:
            try:
                threading.Thread(target=cls.on_change, args=(name, old, new), daemon=True).start()
            except Exception:
                pass

    @classmethod
    def read(cls, name) -> str:
        p = cls._path(name)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @classmethod
    def delete(cls, name):
        cls._path(name).unlink(missing_ok=True)

    @classmethod
    def list(cls) -> dict:
        return {p.name: cls.count(p.name) for p in sorted(PRODUCTS_DIR.glob("*.txt"))}


# ════════════════════════ УТИЛИТЫ ════════════════════════
def fmt_template(text: str, **kw) -> str:
    """$переменные + случайный вариант: «Привет|||Здравствуйте» → один из вариантов."""
    kw.setdefault("date", datetime.now().strftime("%d.%m.%Y"))
    kw.setdefault("time", datetime.now().strftime("%H:%M"))
    out = str(text or "")
    if "|||" in out:
        out = random.choice([x.strip() for x in out.split("|||") if x.strip()] or [out])
    for k in sorted(kw, key=len, reverse=True):
        out = out.replace("$" + k, str(kw[k] if kw[k] is not None else ""))
    return out


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def normalize_proxy(p: str) -> str:
    """socks5://u:p@ip:port · http://ip:port · ip:port · ip:port:user:pass · user:pass@ip:port"""
    p = (p or "").strip()
    if not p:
        return ""
    if "://" in p:
        return p
    parts = p.split(":")
    if "@" not in p and len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    return "http://" + p


def proxies_dict(p: str) -> Optional[dict]:
    p = normalize_proxy(p)
    return {"http": p, "https": p} if p else None


def proxy_label(p: str) -> str:
    return normalize_proxy(p).split("@")[-1] if p else "напрямую"


def check_proxy(p: str, url="https://api.telegram.org", timeout=8) -> tuple:
    t = time.time()
    try:
        requests.get(url, proxies=proxies_dict(p), timeout=timeout)
        return True, round((time.time() - t) * 1000)
    except Exception as e:
        return False, str(e)[:140]


def in_work_hours(cfg: Config) -> bool:
    wh = cfg.get("general.work_hours", {})
    if not wh.get("enabled"):
        return True
    now = datetime.now().strftime("%H:%M")
    a, b = wh.get("from", "00:00"), wh.get("to", "23:59")
    return a <= now <= b if a <= b else (now >= a or now <= b)


def human_uptime(sec) -> str:
    d, sec = divmod(int(sec), 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}д " if d else "") + f"{h}ч {m}м"


def rand_password(n=10):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def make_backup(include_plugins=True) -> Path:
    name = BACKUP_DIR / f"backup_{datetime.now():%Y%m%d_%H%M%S}.zip"
    with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (CONFIG_PATH, STATE_PATH, STATS_PATH, CUSTOMERS_PATH):
            if p.exists():
                z.write(p, f"castor_data/{p.name}")
        for p in PRODUCTS_DIR.glob("*.txt"):
            z.write(p, f"castor_data/products/{p.name}")
        if include_plugins:
            for p in PLUGINS_DIR.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, f"plugins/{p.relative_to(PLUGINS_DIR)}")
    return name


def prune_backups(keep=7):
    files = sorted(BACKUP_DIR.glob("backup_*.zip"))
    for f in files[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)


def _g(obj, *path, default=None):
    """Безопасно достаёт obj.a.b.c (атрибуты или ключи)."""
    cur = obj
    for p in path:
        if cur is None:
            return default
        cur = cur.get(p) if isinstance(cur, dict) else getattr(cur, p, None)
    return default if cur is None else cur


# ════════════════════════ ОБЩИЕ МОДЕЛИ ════════════════════════
class UMessage:
    """Единое сообщение FunPay / Playerok."""

    def __init__(self, platform, chat_id, text, author="", author_id=None, chat_name="",
                 msg_id=None, by_me=False, system=False, raw=None, account=None):
        self.platform, self.chat_id, self.text = platform, chat_id, text or ""
        self.account = account
        self.author, self.author_id, self.chat_name = author or "", author_id, chat_name or author or ""
        self.id, self.by_me, self.system, self.raw = msg_id, by_me, system, raw
        self.time = time.time()
        self._app = None

    def reply(self, text):
        return self._app.send(self.platform, self.chat_id, text, chat_name=self.chat_name,
                              account=self.account) if self._app else False

    def to_dict(self):
        return {"platform": self.platform, "account": self.account, "chat_id": str(self.chat_id), "text": self.text,
                "author": self.author, "chat_name": self.chat_name, "by_me": self.by_me,
                "system": self.system, "time": self.time}


class UOrder:
    def __init__(self, platform, id, title, price=0.0, buyer="", buyer_id=None, chat_id=None,
                 amount=1, status="paid", raw=None, currency="₽", account=None):
        self.platform, self.id, self.title = platform, id, title or ""
        self.account = account
        self.price, self.buyer, self.buyer_id = price, buyer, buyer_id
        try:
            self.amount = max(1, int(amount or 1))
        except (TypeError, ValueError):
            self.amount = 1
        self.chat_id, self.status, self.raw, self.currency = chat_id, status, raw, currency
        self.time = time.time()
        self._app = None

    def reply(self, text):
        if self._app and self.chat_id is not None:
            return self._app.send(self.platform, self.chat_id, text, chat_name=self.buyer, account=self.account)
        return False

    def to_dict(self):
        return {"platform": self.platform, "account": self.account, "id": str(self.id), "title": self.title, "price": self.price,
                "buyer": self.buyer, "chat_id": str(self.chat_id), "amount": self.amount,
                "status": self.status, "time": self.time, "currency": self.currency}
