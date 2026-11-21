"""Аренда аккаунтов — отдельный плагин на каждую игру: RentSteam, Dota 2 Rental, Valorant Rental,
Faceit Rental, VIP Roblox. Общая основа — RentBase; у каждого плагина свои аккаунты, лоты и тексты.

Покупатель оплатил лот аренды → бот выдаёт свободный аккаунт (или ссылку на VIP-сервер), считает время
(количество в заказе × часов за штуку), продлевает при повторной покупке, напоминает о конце.
Команды покупателя: !guard — код Steam Guard, !время — сколько осталось, !замена — попросить другой аккаунт,
!доиграть — (Faceit) добавить время, чтобы закончить матч.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
import uuid
from datetime import datetime

from ..core import esc, fmt_template, load_json, save_json
from . import MOD_DIR, Module

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


class RentBase(Module):
    GROUP = "Аренда"
    ORDER = 30
    GAME = ""
    GUARD = True               # коды Steam Guard по !guard
    LINK_MODE = False          # VIP Roblox: выдаётся ссылка, а не логин/пароль
    EXTRA_TIME = False         # Faceit: !доиграть
    DEFAULTS = {
        "lots": [],
        "accounts": [],
        "guard_command": "!guard|!гуард",
        "time_command": "!время|!time",
        "replace_command": "!замена|!replace",
        "extra_command": "!доиграть",
        "extra_minutes": 40,
        "remind_before_min": 15,
        "after_end": "ask",
        "text_start": ("Аренда начата ✅\nЛогин: $login\nПароль: $password\nДо: $end ($hours ч)\n\n"
                       "Сколько осталось — !время. Проблема с аккаунтом — !замена.\nНе меняйте данные аккаунта."),
        "text_extend": "Аренда продлена на $hours ч ✅ Теперь до $end.",
        "text_remind": "⏰ До конца аренды осталось $left. Продлить — оплатите лот ещё раз.",
        "text_end": "Аренда закончилась. Спасибо! Буду рад отзыву ⭐",
        "text_wait": "Все аккаунты сейчас заняты — выдам, как только освободится. Продавец уже в курсе.",
    }
    BASE_FIELDS = [
        ("lots", "Какие лоты — эта аренда", "items"),
        ("accounts", "Аккаунты", "items"),
        ("after_end", "Когда аренда кончилась", "select", [["ask", "ждать, пока я сменю пароль"], ["free", "сразу выдавать снова"]]),
        ("remind_before_min", "Напомнить до конца, мин", "number"),
        ("time_command", "Команда «сколько осталось»", "text"),
        ("replace_command", "Команда «замена аккаунта»", "text"),
        ("text_start", "Сообщение при выдаче", "area", "$login $password $link $end $hours"),
        ("text_extend", "Сообщение при продлении", "area"),
        ("text_remind", "Напоминание о конце", "area", "$left — сколько осталось"),
        ("text_end", "Сообщение в конце", "area"),
        ("text_wait", "Если свободных аккаунтов нет", "area"),
    ]
    LOT_FIELDS = [("keyword", "Слово в названии лота", "text", "например: аренда | rent"),
                  ("hours_per_unit", "Часов за 1 шт. в заказе", "number")]
    ACC_FIELDS = [("login", "Логин", "text"), ("password", "Пароль", "secret"),
                  ("shared_secret", "shared_secret Steam Guard", "secret", "из maFile — для кодов !guard"),
                  ("note", "Заметка (видна только вам)", "text"), ("enabled", "Используется", "bool")]

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        f = list(cls.BASE_FIELDS)
        if cls.GUARD:
            f.insert(4, ("guard_command", "Команда кода Steam Guard", "text"))
        if cls.EXTRA_TIME:
            f.insert(5, ("extra_command", "Команда «доиграть матч»", "text"))
            f.insert(6, ("extra_minutes", "Сколько минут добавить, чтобы доиграть", "number"))
        cls.FIELDS = f
        cls.ITEM_FIELDS = {"lots": cls.LOT_FIELDS, "accounts": cls.ACC_FIELDS}

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("rentals", {})
        self.data.setdefault("dirty", [])
        self.data.setdefault("queue", [])
        self.data.setdefault("extra_used", [])

    # ── аккаунты ──
    def _ensure_ids(self):
        path = f"modules.{self.KEY}.accounts"
        accs = self.app.cfg.get(path, []) or []
        if any(not a.get("id") for a in accs):
            for a in accs:
                a.setdefault("id", uuid.uuid4().hex[:8])
            self.app.cfg.set(path, accs)

    def _accounts(self):
        self._ensure_ids()
        return [a for a in self.get("accounts", []) if a.get("id") and a.get("enabled", True)]

    def account(self, acc_id):
        return next((a for a in self.get("accounts", []) if a.get("id") == acc_id), None)

    def _busy_ids(self):
        return {r["account_id"] for r in self.data["rentals"].values() if not r.get("ended")} | set(self.data["dirty"])

    def _free(self, exclude=()):
        busy = self._busy_ids() | set(exclude)
        return [a for a in self._accounts() if a["id"] not in busy]

    def _lot(self, title):
        t = (title or "").lower()
        for lot in self.get("lots", []):
            kws = [k.strip().lower() for k in str(lot.get("keyword", "")).split("|") if k.strip()]
            if kws and any(k in t for k in kws):
                return lot
        return None

    def active_for(self, platform, buyer):
        for rid, r in self.data["rentals"].items():
            if not r.get("ended") and r["platform"] == platform and str(r["buyer"]).lower() == str(buyer).lower():
                return rid, r
        return None, None

    def _vars(self, r, acc, hours=None):
        return dict(login=acc.get("login", ""), password=acc.get("password", ""), link=acc.get("link", ""),
                    game=self.GAME, username=r.get("buyer", ""), left=human_left(r["end"] - time.time()),
                    end=datetime.fromtimestamp(r["end"]).strftime("%d.%m %H:%M"),
                    hours=f"{round(hours if hours is not None else (r['end'] - r['start']) / 3600, 1):g}")

    # ── заказ ──
    def on_order(self, o) -> bool:
        lot = self._lot(o.title)
        if not lot:
            return False
        hours = float(lot.get("hours_per_unit") or 1) * max(1, int(o.amount or 1))
        with self.lock:
            rid, r = self.active_for(o.platform, o.buyer)
            if r:
                r["end"] += hours * 3600
                r["reminded"] = False
                r["orders"].append(str(o.id))
                self.save()
                acc = self.account(r["account_id"]) or {}
                o.reply(fmt_template(self.get("text_extend"), **self._vars(r, acc, hours)))
                self.app.stats.set_order_flag(o.id, "delivered", True, o.platform)
                self.notify(f"{self.ICON} {esc(self.NAME)}: продлено {esc(o.buyer)} +{hours:g} ч")
                return True
            free = self._free()
            if not free:
                self.data["queue"].append({"platform": o.platform, "account": o.account, "order_id": str(o.id),
                                           "chat_id": o.chat_id, "buyer": o.buyer, "hours": hours, "t": time.time()})
                self.save()
                o.reply(fmt_template(self.get("text_wait"), username=o.buyer))
                self.notify(f"{self.ICON} <b>{esc(self.NAME)}: нет свободных аккаунтов</b> для {esc(o.buyer)} "
                            f"(заказ <code>{esc(o.id)}</code>). Выдам сам, когда освободится.")
                return True
            self._start(free[0], o.platform, o.account, str(o.id), o.chat_id, o.buyer, hours, reply=o.reply)
        return True

    def _start(self, acc, platform, account, order_id, chat_id, buyer, hours, reply=None):
        now = time.time()
        r = {"account_id": acc["id"], "platform": platform, "account": account, "chat_id": str(chat_id), "buyer": buyer,
             "start": now, "end": now + hours * 3600, "orders": [order_id], "reminded": False, "ended": False}
        self.data["rentals"][uuid.uuid4().hex[:10]] = r
        self.save()
        text = fmt_template(self.get("text_start"), **self._vars(r, acc, hours))
        (reply or (lambda t: self.reply(platform, chat_id, t, account, buyer)))(text)
        self.app.stats.set_order_flag(order_id, "delivered", True, platform)
        self.notify(f"{self.ICON} <b>{esc(self.NAME)}</b>: выдано {esc(buyer)} — "
                    f"{esc(acc.get('login') or acc.get('note') or acc['id'])} на {hours:g} ч")

    # ── команды покупателя ──
    @staticmethod
    def _is(cmds, text):
        t = (text or "").strip().lower()
        return any(t == c.strip().lower() for c in str(cmds).split("|") if c.strip())

    def on_message(self, m) -> bool:
        if m.system or m.by_me:
            return False
        say = lambda t: self.reply(m.platform, m.chat_id, t, m.account, m.chat_name)  # noqa: E731
        is_guard = self.GUARD and self._is(self.get("guard_command"), m.text)
        is_time = self._is(self.get("time_command"), m.text)
        is_repl = self._is(self.get("replace_command"), m.text)
        is_extra = self.EXTRA_TIME and self._is(self.get("extra_command"), m.text)
        if not (is_guard or is_time or is_repl or is_extra):
            return False
        rid, r = self.active_for(m.platform, m.author)
        if not r:
            # аренда может быть в другом плагине аренды — тогда ответит он
            others = [x for x in self.app.modules.active() if isinstance(x, RentBase) and x is not self]
            if any(x.active_for(m.platform, m.author)[1] for x in others):
                return False
            say("У вас нет активной аренды.")
            return True
        acc = self.account(r["account_id"]) or {}
        if is_guard:
            if not acc.get("shared_secret"):
                say("Для этого аккаунта код не нужен — заходите по логину и паролю.")
                return True
            try:
                code = steam_guard_code(acc["shared_secret"])
            except Exception:
                self.notify(f"{self.ICON} Неверный shared_secret у {esc(acc.get('login'))} — код не создать", kind="errors")
                say("Не получилось создать код, продавец уже в курсе.")
                return True
            say(f"Код Steam Guard: {code}\nДействует ещё {30 - int(time.time()) % 30} сек.")
        elif is_time:
            say(f"До конца аренды: {human_left(r['end'] - time.time())} (до {datetime.fromtimestamp(r['end']):%d.%m %H:%M})")
        elif is_repl:
            self.notify(f"{self.ICON} <b>{esc(m.author)} просит замену аккаунта</b> ({esc(self.NAME)}): "
                        f"{esc(acc.get('login') or acc.get('note') or '')}",
                        buttons=[[("🔁 Выдать другой", f"duo:mod:{self.KEY}:act:swap:{rid}", "delivery")]])
            say("Передал продавцу — он проверит аккаунт и выдаст замену.")
        elif is_extra:
            if rid in self.data["extra_used"]:
                say("Дополнительное время уже добавлялось к этой аренде.")
            else:
                mins = float(self.get("extra_minutes", 40) or 40)
                r["end"] += mins * 60
                r["reminded"] = True
                self.data["extra_used"].append(rid)
                self.save()
                say(f"Добавил {mins:g} мин, чтобы вы доиграли матч ✅ Теперь до {datetime.fromtimestamp(r['end']):%H:%M}.")
        return True

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
                    name = esc(acc.get("login") or acc.get("note") or acc.get("id", ""))
                    if self.get("after_end", "ask") == "ask":
                        if r["account_id"] not in self.data["dirty"]:
                            self.data["dirty"].append(r["account_id"])
                        what = "Смените ссылку VIP-сервера" if self.LINK_MODE else "Смените пароль аккаунта"
                        self.notify(f"{self.ICON} <b>{esc(self.NAME)}: аренда закончилась</b> — {esc(r['buyer'])} · {name}\n"
                                    f"{what} и нажмите кнопку — он снова пойдёт в аренду.",
                                    buttons=[[("✅ Готово, выдавать снова", f"duo:mod:{self.KEY}:free:{r['account_id']}", "delivery")]])
                    else:
                        self.notify(f"{self.ICON} {esc(self.NAME)}: аренда закончилась — {esc(r['buyer'])} · {name}")
            for rid, r in list(self.data["rentals"].items()):
                if r.get("ended") and now - r["end"] > 7 * 86400:
                    del self.data["rentals"][rid]
            self.save()
        self._serve_queue()

    def _serve_queue(self):
        with self.lock:
            for item in list(self.data["queue"]):
                free = self._free()
                if not free:
                    break
                self.data["queue"].remove(item)
                self.save()
                self._start(free[0], item["platform"], item["account"], item["order_id"], item["chat_id"],
                            item["buyer"], item["hours"])

    # ── меню ──
    def status(self) -> str:
        act = sum(1 for r in self.data["rentals"].values() if not r.get("ended"))
        s = f"в аренде {act} из {len(self.get('accounts', []))}"
        if self.data["dirty"]:
            s += f" · ждут смены пароля: {len(self.data['dirty'])}"
        if self.data["queue"]:
            s += f" · в очереди: {len(self.data['queue'])}"
        return s

    def actions(self) -> list:
        out = [("list", "📋 Активные аренды")]
        out += [(f"free:{a}", f"✅ {((self.account(a) or {}).get('login') or a)[:20]}: снова выдавать") for a in self.data["dirty"]]
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
            return "\n".join(f"• {r['buyer']} · {(self.account(r['account_id']) or {}).get('login', '?')} · "
                             f"осталось {human_left(r['end'] - time.time())}" for r in act) or "Активных аренд нет"
        if action == "swap" and args:
            with self.lock:
                r = self.data["rentals"].get(args[0])
                if not r or r.get("ended"):
                    return "Аренда уже закончилась"
                free = self._free(exclude=[r["account_id"]])
                if not free:
                    return "Нет свободного аккаунта для замены"
                if r["account_id"] not in self.data["dirty"]:
                    self.data["dirty"].append(r["account_id"])
                r["account_id"] = free[0]["id"]
                self.save()
                acc = free[0]
            self.reply(r["platform"], r["chat_id"], "Замена аккаунта ✅\n" +
                       fmt_template(self.get("text_start"), **self._vars(r, acc)), r["account"], r["buyer"])
            return f"Выдан другой аккаунт: {acc.get('login') or acc.get('note')}"
        return "Нет такого действия"


class RentSteamModule(RentBase):
    KEY = "rentsteam"
    NAME = "RentSteam"
    ICON = "🎮"
    GAME = "Steam"
    DESCRIPTION = ("Аренда своих Steam-аккаунтов: логин и пароль, коды Steam Guard по !guard, учёт времени, "
                   "продление повторной покупкой, замена аккаунта.")
    DEFAULTS = dict(RentBase.DEFAULTS, text_start=("Аренда начата ✅\nЛогин: $login\nПароль: $password\nДо: $end ($hours ч)\n\n"
                                                   "Код Steam Guard — !guard. Сколько осталось — !время. "
                                                   "Проблема с аккаунтом — !замена.\nНе меняйте данные аккаунта."))


class Dota2RentalModule(RentBase):
    KEY = "dota2rental"
    NAME = "Dota 2 Rental"
    ICON = "🛡"
    GAME = "Dota 2"
    DESCRIPTION = ("Аренда своих аккаунтов Dota 2: логин, пароль и Steam Guard, таймер, продление, замена аккаунта "
                   "по !замена (low priority, бан).")
    DEFAULTS = dict(RentBase.DEFAULTS, text_start=("Аренда начата ✅\nЛогин: $login\nПароль: $password\nДо: $end ($hours ч)\n\n"
                                                   "Код Steam Guard — !guard. Сколько осталось — !время. "
                                                   "Проблема с аккаунтом — !замена.\nНе меняйте данные аккаунта."))


class ValorantRentalModule(RentBase):
    KEY = "valorantrental"
    NAME = "Valorant Rental"
    ICON = "🎯"
    GAME = "Valorant"
    GUARD = False
    DESCRIPTION = ("Аренда аккаунтов Valorant: таймер, продление, коды входа Riot с почты по !код (включите EmailCode "
                   "и укажите ящик у аккаунта). Пароль после аренды меняете вы.")
    ACC_FIELDS = [("login", "Логин Riot", "text"), ("password", "Пароль", "secret"),
                  ("mailbox", "Почтовый ящик (имя из EmailCode)", "text", "для кодов входа Riot"),
                  ("note", "Заметка", "text"), ("enabled", "Используется", "bool")]
    DEFAULTS = dict(RentBase.DEFAULTS, text_start=("Аренда Valorant начата ✅\nЛогин: $login\nПароль: $password\n"
                                                   "До: $end ($hours ч)\n\nПопросит код с почты — напишите !код. "
                                                   "Сколько осталось — !время."))


class FaceitRentalModule(RentBase):
    KEY = "faceitrental"
    NAME = "Faceit Rental"
    ICON = "🔥"
    GAME = "FACEIT"
    GUARD = False
    EXTRA_TIME = True
    DESCRIPTION = ("Аренда аккаунтов FACEIT: таймер и продление, !доиграть — один раз добавить время, чтобы закончить "
                   "текущий матч, замена аккаунта при бане.")
    DEFAULTS = dict(RentBase.DEFAULTS,
                    text_remind="⏰ До конца аренды $left. Не успеваете доиграть матч — напишите !доиграть.")


class VipRobloxModule(RentBase):
    KEY = "viproblox"
    NAME = "VIP Roblox"
    ICON = "🧱"
    GAME = "Roblox"
    GUARD = False
    LINK_MODE = True
    DESCRIPTION = "Аренда VIP-серверов Roblox: выдаёт ссылку на сервер, считает время доступа, напоминает и продлевает."
    ACC_FIELDS = [("note", "Название сервера (игра)", "text", "например: Blox Fruits VIP #1"),
                  ("link", "Ссылка на VIP-сервер", "text"), ("enabled", "Используется", "bool")]
    DEFAULTS = dict(RentBase.DEFAULTS,
                    text_start="Доступ к VIP-серверу ✅\n$link\nДо: $end ($hours ч)\n\nСколько осталось — !время.",
                    text_end="Доступ к VIP-серверу закончился. Спасибо! Буду рад отзыву ⭐")


RENTALS = (RentSteamModule, Dota2RentalModule, ValorantRentalModule, FaceitRentalModule, VipRobloxModule)


def migrate_old_rent(app):
    """Старый общий модуль «Аренда аккаунтов» (modules.rent) переносим в RentSteam."""
    mods = app.cfg.data.get("modules") or {}
    old = mods.get("rent")
    if not isinstance(old, dict):
        return
    new = dict(old)
    new["accounts"] = [{k: v for k, v in a.items() if k != "game"} for a in old.get("accounts", [])]
    new["lots"] = [{k: v for k, v in x.items() if k != "game"} for x in old.get("lots", [])]
    mods["rentsteam"] = new
    mods.pop("rent", None)
    app.cfg.save()
    src = MOD_DIR / "rent.json"
    if src.exists() and not (MOD_DIR / "rentsteam.json").exists():
        save_json(MOD_DIR / "rentsteam.json", load_json(src, {}))
