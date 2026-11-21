"""Аренда аккаунтов: Steam, Dota 2, Valorant, FACEIT, VIP-серверы Roblox и любые другие.

Покупатель оплатил лот аренды → бот выдаёт свободный аккаунт нужной игры, считает время
(количество в заказе × часов за единицу), продлевает при повторной покупке, напоминает о конце,
по команде !guard присылает код Steam Guard, по !время — сколько осталось.
Когда аренда кончилась — пишет покупателю и вам: «смените пароль», и аккаунт снова в работе
после нажатия кнопки «Пароль сменён» (или сразу, если так настроено).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
import uuid
from datetime import datetime

from ..core import esc, fmt_template, log
from . import Module

STEAM_CHARS = "23456789BCDFGHJKMNPQRTVWXY"


def steam_guard_code(shared_secret: str, t: float = None) -> str:
    """Код Steam Guard (как в мобильном приложении) из shared_secret аккаунта."""
    key = base64.b64decode(shared_secret.strip())
    counter = int((t or time.time()) // 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 0x0F
    full = struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF
    code = ""
    for _ in range(5):
        code += STEAM_CHARS[full % len(STEAM_CHARS)]
        full //= len(STEAM_CHARS)
    return code


def human_left(sec: float) -> str:
    sec = max(0, int(sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    return (f"{d} д " if d else "") + (f"{h} ч " if h or d else "") + f"{m} мин"


class RentModule(Module):
    KEY = "rent"
    NAME = "Аренда аккаунтов"
    ICON = "🎮"
    DESCRIPTION = ("Выдаёт аккаунты в аренду по таймеру: Steam, Dota 2, Valorant, FACEIT, VIP Roblox и любые другие. "
                   "Продление, напоминания, коды Steam Guard по команде !guard.")
    DEFAULTS = {
        "lots": [],        # [{"keyword": "аренда dota", "game": "dota", "hours_per_unit": 1}]
        "accounts": [],    # [{"id", "game", "login", "password", "shared_secret", "mailbox", "note", "enabled"}]
        "guard_command": "!guard|!гуард|!код стим",
        "time_command": "!время|!time",
        "remind_before_min": 15,
        "after_end": "ask",   # ask — ждать вашего «пароль сменён»; free — сразу вернуть в работу
        "text_start": ("Аренда начата ✅\nЛогин: $login\nПароль: $password\nДо: $end ($hours ч)\n\n"
                       "Код Steam Guard — команда !guard. Сколько осталось — !время.\nНе меняйте данные аккаунта."),
        "text_extend": "Аренда продлена на $hours ч ✅ Теперь до $end.",
        "text_remind": "⏰ До конца аренды осталось $left. Продлить — оплатите лот ещё раз.",
        "text_end": "Аренда закончилась. Спасибо! Буду рад отзыву ⭐",
        "text_wait": "Все аккаунты сейчас заняты — выдам, как только освободится. Продавец уже в курсе.",
    }
    FIELDS = [
        ("lots", "Какие лоты — аренда", "items"),
        ("accounts", "Аккаунты для аренды", "items"),
        ("after_end", "Когда аренда кончилась", "select", [["ask", "ждать, пока я сменю пароль"], ["free", "сразу выдавать снова"]]),
        ("remind_before_min", "Напомнить до конца, мин", "number"),
        ("guard_command", "Команда кода Steam Guard", "text"),
        ("time_command", "Команда «сколько осталось»", "text"),
        ("text_start", "Сообщение при выдаче", "area", "$login $password $end $hours $game"),
        ("text_extend", "Сообщение при продлении", "area"),
        ("text_remind", "Напоминание о конце", "area", "$left — сколько осталось"),
        ("text_end", "Сообщение в конце", "area"),
        ("text_wait", "Если свободных аккаунтов нет", "area"),
    ]
    ITEM_FIELDS = {
        "lots": [("keyword", "Слово в названии лота", "text", "например: аренда dota | rent dota"),
                 ("game", "Игра (метка аккаунтов)", "text", "steam, dota, valorant…"),
                 ("hours_per_unit", "Часов за 1 шт. в заказе", "number")],
        "accounts": [("game", "Игра (метка)", "text"), ("login", "Логин", "text"), ("password", "Пароль", "secret"),
                     ("shared_secret", "shared_secret Steam Guard", "secret", "из maFile, для кодов !guard"),
                     ("mailbox", "Почтовый ящик (для кодов)", "text", "имя ящика из модуля «Коды с почты»"),
                     ("note", "Заметка", "text"), ("enabled", "Используется", "bool")],
    }

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("rentals", {})      # id → аренда
        self.data.setdefault("dirty", [])        # аккаунты, ждущие смены пароля
        self.data.setdefault("queue", [])        # заказы, ждущие свободный аккаунт

    # ── поиск ──
    def _lot(self, title):
        t = (title or "").lower()
        for lot in self.get("lots", []):
            kws = [k.strip().lower() for k in str(lot.get("keyword", "")).split("|") if k.strip()]
            if kws and any(k in t for k in kws):
                return lot
        return None

    def _accounts(self, game):
        self._ensure_ids()
        return [a for a in self.get("accounts", []) if a.get("id") and a.get("enabled", True) and
                str(a.get("game", "")).lower() == str(game).lower()]

    def _busy_ids(self):
        busy = {r["account_id"] for r in self.data["rentals"].values() if not r.get("ended")}
        return busy | set(self.data["dirty"])

    def _active_for(self, platform, buyer, game=None):
        for rid, r in self.data["rentals"].items():
            if r.get("ended") or r["platform"] != platform or str(r["buyer"]).lower() != str(buyer).lower():
                continue
            if game is None or str(r["game"]).lower() == str(game).lower():
                return rid, r
        return None, None

    def account(self, acc_id):
        return next((a for a in self.get("accounts", []) if a.get("id") == acc_id or a.get("login") == acc_id), None)

    def _ensure_ids(self):
        accs = self.app.cfg.get("modules.rent.accounts", []) or []
        changed = False
        for a in accs:
            if not a.get("id"):
                a["id"] = uuid.uuid4().hex[:8]
                changed = True
        if changed:
            self.app.cfg.set("modules.rent.accounts", accs)

    def _vars(self, r, acc, hours=None):
        return dict(login=acc.get("login", ""), password=acc.get("password", ""), game=r.get("game", ""),
                    end=datetime.fromtimestamp(r["end"]).strftime("%d.%m %H:%M"),
                    hours=hours if hours is not None else round((r["end"] - r["start"]) / 3600, 1),
                    username=r.get("buyer", ""), left=human_left(r["end"] - time.time()))

    # ── заказ ──
    def on_order(self, o) -> bool:
        lot = self._lot(o.title)
        if not lot:
            return False
        self._ensure_ids()
        hours = float(lot.get("hours_per_unit") or 1) * max(1, int(o.amount or 1))
        game = lot.get("game", "")
        with self.lock:
            rid, r = self._active_for(o.platform, o.buyer, game)
            if r:  # продление
                r["end"] += hours * 3600
                r["reminded"] = False
                r["orders"].append(str(o.id))
                self.save()
                acc = self.account(r["account_id"]) or {}
                o.reply(fmt_template(self.get("text_extend"), **self._vars(r, acc, hours)))
                self.app.stats.set_order_flag(o.id, "delivered", True, o.platform)
                self.notify(f"🎮 Аренда продлена: <b>{esc(o.buyer)}</b> +{hours:g} ч · {esc(acc.get('login', ''))}")
                return True
            free = [a for a in self._accounts(game) if a["id"] not in self._busy_ids()]
            if not free:
                self.data["queue"].append({"platform": o.platform, "account": o.account, "order_id": str(o.id),
                                           "chat_id": o.chat_id, "buyer": o.buyer, "game": game, "hours": hours,
                                           "t": time.time()})
                self.save()
                o.reply(fmt_template(self.get("text_wait"), username=o.buyer))
                self.notify(f"🎮 <b>Нет свободных аккаунтов</b> «{esc(game)}» для {esc(o.buyer)} "
                            f"(заказ <code>{esc(o.id)}</code>). Выдам автоматически, когда освободится.")
                return True
            self._start(free[0], o.platform, o.account, str(o.id), o.chat_id, o.buyer, game, hours, reply=o.reply)
        return True

    def _start(self, acc, platform, account, order_id, chat_id, buyer, game, hours, reply=None):
        now = time.time()
        rid = uuid.uuid4().hex[:10]
        r = {"account_id": acc["id"], "platform": platform, "account": account, "chat_id": str(chat_id),
             "buyer": buyer, "game": game, "start": now, "end": now + hours * 3600, "orders": [order_id],
             "reminded": False, "ended": False}
        self.data["rentals"][rid] = r
        self.save()
        text = fmt_template(self.get("text_start"), **self._vars(r, acc, hours))
        if reply:
            reply(text)
        else:
            self.reply(platform, chat_id, text, account, buyer)
        self.app.stats.set_order_flag(order_id, "delivered", True, platform)
        self.notify(f"🎮 <b>Аренда выдана</b>: {esc(buyer)} · {esc(game)} · {esc(acc.get('login'))} на {hours:g} ч")

    # ── сообщения ──
    @staticmethod
    def _is(cmd_list, text):
        t = (text or "").strip().lower()
        return any(t == c.strip().lower() for c in str(cmd_list).split("|") if c.strip())

    def on_message(self, m) -> bool:
        if self._is(self.get("guard_command"), m.text):
            rid, r = self._active_for(m.platform, m.author)
            if not r:
                self.reply(m.platform, m.chat_id, "У вас нет активной аренды.", m.account, m.chat_name)
                return True
            acc = self.account(r["account_id"]) or {}
            if not acc.get("shared_secret"):
                self.reply(m.platform, m.chat_id, "Для этого аккаунта код не нужен — заходите по логину и паролю.",
                           m.account, m.chat_name)
                return True
            try:
                code = steam_guard_code(acc["shared_secret"])
            except Exception:
                self.notify(f"🎮 Неверный shared_secret у аккаунта {esc(acc.get('login'))} — код не создать")
                self.reply(m.platform, m.chat_id, "Не получилось создать код, продавец уже в курсе.", m.account, m.chat_name)
                return True
            left = 30 - int(time.time()) % 30
            self.reply(m.platform, m.chat_id, f"Код Steam Guard: {code}\nДействует ещё {left} сек.", m.account, m.chat_name)
            return True
        if self._is(self.get("time_command"), m.text):
            rid, r = self._active_for(m.platform, m.author)
            text = (f"До конца аренды: {human_left(r['end'] - time.time())} "
                    f"(до {datetime.fromtimestamp(r['end']):%d.%m %H:%M})") if r else "У вас нет активной аренды."
            self.reply(m.platform, m.chat_id, text, m.account, m.chat_name)
            return True
        return False

    # ── время ──
    def tick(self):
        now = time.time()
        before = float(self.get("remind_before_min", 15) or 0) * 60
        with self.lock:
            for rid, r in list(self.data["rentals"].items()):
                if r.get("ended"):
                    continue
                acc = self.account(r["account_id"]) or {}
                if before and not r.get("reminded") and 0 < r["end"] - now <= before:
                    r["reminded"] = True
                    self.reply(r["platform"], r["chat_id"], fmt_template(self.get("text_remind"), **self._vars(r, acc)),
                               r["account"], r["buyer"])
                if now >= r["end"]:
                    r["ended"] = True
                    self.reply(r["platform"], r["chat_id"], fmt_template(self.get("text_end"), **self._vars(r, acc)),
                               r["account"], r["buyer"])
                    if self.get("after_end", "ask") == "ask":
                        if r["account_id"] not in self.data["dirty"]:
                            self.data["dirty"].append(r["account_id"])
                        self.notify(f"🎮 <b>Аренда закончилась</b>: {esc(r['buyer'])} · {esc(acc.get('login'))}\n"
                                    f"Смените пароль аккаунта и нажмите кнопку — он снова пойдёт в аренду.",
                                    buttons=[[("✅ Пароль сменён", f"duo:mod:rent:free:{r['account_id']}", "delivery")]])
                    else:
                        self.notify(f"🎮 Аренда закончилась: {esc(r['buyer'])} · {esc(acc.get('login'))}")
            # старые записи чистим через неделю
            for rid, r in list(self.data["rentals"].items()):
                if r.get("ended") and now - r["end"] > 7 * 86400:
                    del self.data["rentals"][rid]
            self.save()
        self._serve_queue()

    def _serve_queue(self):
        with self.lock:
            q = self.data["queue"]
            for item in list(q):
                free = [a for a in self._accounts(item["game"]) if a["id"] not in self._busy_ids()]
                if not free:
                    continue
                q.remove(item)
                self.save()
                self._start(free[0], item["platform"], item["account"], item["order_id"], item["chat_id"],
                            item["buyer"], item["game"], item["hours"])

    # ── меню ──
    def status(self) -> str:
        act = [r for r in self.data["rentals"].values() if not r.get("ended")]
        total = len(self.get("accounts", []))
        s = f"в аренде {len(act)} из {total}"
        if self.data["dirty"]:
            s += f" · ждут смены пароля: {len(self.data['dirty'])}"
        if self.data["queue"]:
            s += f" · в очереди: {len(self.data['queue'])}"
        return s

    def actions(self) -> list:
        out = [("list", "📋 Активные аренды")]
        out += [(f"free:{a}", f"✅ {(self.account(a) or {}).get('login', a)[:20]}: пароль сменён") for a in self.data["dirty"]]
        return out

    def run_action(self, action: str, args: list) -> str:
        if action == "free" and args:
            with self.lock:
                if args[0] in self.data["dirty"]:
                    self.data["dirty"].remove(args[0])
                    self.save()
            self._serve_queue()
            return "Аккаунт снова в работе"
        if action == "list":
            act = [r for r in self.data["rentals"].values() if not r.get("ended")]
            if not act:
                return "Активных аренд нет"
            return "\n".join(f"• {r['buyer']} · {r['game']} · {(self.account(r['account_id']) or {}).get('login', '?')} · "
                             f"осталось {human_left(r['end'] - time.time())}" for r in act)
        if action == "stop" and args:
            with self.lock:
                r = self.data["rentals"].get(args[0])
                if r:
                    r["end"] = time.time()
            self.tick()
            return "Аренда завершена"
        return "Нет такого действия"
