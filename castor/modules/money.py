"""Модули денег и безопасности: цели продаж, выгрузка в Excel, чеки для самозанятых,
резервная копия в Telegram, проверка подозрительных покупателей."""
from __future__ import annotations

import csv
import time
from datetime import datetime

from ..core import DATA_DIR, esc, make_backup
from . import Module, continue_order, order_from_dict, order_to_dict, send_file_to_owners


def _rub(v) -> str:
    return f"{float(v or 0):,.0f}".replace(",", " ") + " ₽"


# ═════════════════════ цели продаж ═════════════════════
class GoalsModule(Module):
    KEY = "goals"
    GROUP = "Деньги"
    NAME = "Goals"
    ICON = "🎯"
    DESCRIPTION = "Ставите цель выручки на день и на месяц — бот показывает прогресс и поздравляет, когда цель взята."
    DEFAULTS = {"day": 3000, "month": 60000, "by_profit": False}
    FIELDS = [("day", "Цель на день, ₽", "number"), ("month", "Цель на месяц, ₽", "number"),
              ("by_profit", "Считать по прибыли, а не по выручке", "bool")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("hit", {})

    def _values(self):
        s = self.app.stats
        key = "profit" if self.get("by_profit") else "sum"
        start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        return s.today()["total"][key], s.period(time.time() - start + 1)["total"][key]

    def tick(self):
        day, month = self._values()
        today, mon = datetime.now().strftime("%Y%m%d"), datetime.now().strftime("%Y%m")
        what = "прибыли" if self.get("by_profit") else "выручки"
        if self.get("day") and day >= float(self.get("day")) and self.data["hit"].get("day") != today:
            self.data["hit"]["day"] = today
            self.save()
            self.notify(f"🎯 <b>Цель дня взята!</b> {_rub(day)} {what} при цели {_rub(self.get('day'))} 🎉", kind="finance")
        if self.get("month") and month >= float(self.get("month")) and self.data["hit"].get("month") != mon:
            self.data["hit"]["month"] = mon
            self.save()
            self.notify(f"🏆 <b>Цель месяца взята!</b> {_rub(month)} {what} при цели {_rub(self.get('month'))}", kind="finance")

    def status(self) -> str:
        day, month = self._values()
        d, m = float(self.get("day") or 0), float(self.get("month") or 0)
        bar = lambda v, g: f"{min(100, round(v / g * 100)) if g else 0}%"  # noqa: E731
        return f"день {_rub(day)} из {_rub(d)} ({bar(day, d)}) · месяц {_rub(month)} из {_rub(m)} ({bar(month, m)})"


# ═════════════════════ выгрузка в Excel ═════════════════════
class ExportModule(Module):
    KEY = "export"
    GROUP = "Деньги"
    NAME = "ExportCSV"
    ICON = "📤"
    DESCRIPTION = "Раз в день присылает файл со всеми заказами, выручкой и прибылью — открывается в Excel и Google Таблицах."
    DEFAULTS = {"time": "23:58", "days": 30, "daily": True}
    FIELDS = [("daily", "Присылать каждый день", "bool"), ("time", "Во сколько", "text", "например 23:58"),
              ("days", "За сколько последних дней", "number")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("sent", "")

    def build(self) -> str:
        out = DATA_DIR / "exports"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"orders_{datetime.now():%Y%m%d_%H%M}.csv"
        since = time.time() - float(self.get("days", 30) or 30) * 86400
        s = self.app.stats
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Дата", "Площадка", "Аккаунт", "Заказ", "Товар", "Покупатель", "Кол-во", "Цена",
                        "Комиссия", "Себестоимость", "Прибыль", "Выдан", "Возврат"])
            for o in s.data.get("orders", []):
                if o.get("t", 0) < since:
                    continue
                w.writerow([datetime.fromtimestamp(o["t"]).strftime("%d.%m.%Y %H:%M"),
                            "FunPay" if o["p"] == "funpay" else "Playerok", self.app.account_name(o.get("a") or ""),
                            o["id"], o.get("title", ""), o.get("buyer", ""), o.get("amount", 1),
                            str(o.get("price", 0)).replace(".", ","), str(o.get("commission") or 0).replace(".", ","),
                            "" if o.get("cost") is None else str(o["cost"]).replace(".", ","),
                            str(round(s.profit_of(o), 2)).replace(".", ","),
                            "да" if o.get("delivered") else "нет", "да" if o.get("refunded") else "нет"])
        return str(path)

    def tick(self):
        if not self.get("daily", True):
            return
        today = datetime.now().strftime("%Y%m%d")
        if datetime.now().strftime("%H:%M") >= str(self.get("time") or "23:58") and self.data["sent"] != today:
            self.data["sent"] = today
            self.save()
            send_file_to_owners(self.app, self.build(), f"📤 Заказы за {self.get('days')} дн. — откройте в Excel")

    def actions(self) -> list:
        return [("now", "📤 Прислать файл сейчас")]

    def run_action(self, action, args) -> str:
        if action == "now":
            n = send_file_to_owners(self.app, self.build(), f"📤 Заказы за {self.get('days')} дн.")
            return "Файл отправлен в Telegram" if n else "Не удалось отправить: Telegram-бот не подключён"
        return "Нет такого действия"

    def status(self) -> str:
        return f"каждый день в {self.get('time')}" if self.get("daily", True) else "по кнопке"


# ═════════════════════ чеки для самозанятых ═════════════════════
class ReceiptsModule(Module):
    KEY = "receipts"
    GROUP = "Деньги"
    NAME = "MyTax"
    ICON = "🧾"
    DESCRIPTION = ("Для самозанятых: напоминает пробить чек по каждой подтверждённой продаже — с суммой и названием, "
                   "кнопкой «Чек пробит». Сам чеки не пробивает: это делается в приложении «Мой налог».")
    DEFAULTS = {"mode": "daily", "time": "21:00", "service_name": "Цифровой товар: $title"}
    FIELDS = [("mode", "Когда напоминать", "select", [["each", "по каждой продаже"], ["daily", "одним списком раз в день"]]),
              ("time", "Время списка", "text", "например 21:00"),
              ("service_name", "Название услуги в чеке", "text", "$title — название заказа")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("since", time.time())
        self.data.setdefault("done", {})       # order_id → t
        self.data.setdefault("notified", {})
        self.data.setdefault("day", "")
        self._last = 0

    def _todo(self):
        open_ids = {v.get("id") for v in self.app.state.data.get("open_orders", {}).values()}
        return [o for o in self.app.stats.data.get("orders", []) if o.get("t", 0) >= self.data["since"]
                and o.get("delivered") and not o.get("refunded") and o["id"] not in open_ids and o["id"] not in self.data["done"]]

    def _line(self, o):
        name = self.get("service_name", "$title").replace("$title", o.get("title", "")[:60])
        return f"{name} — {_rub(o.get('price'))}"

    def tick(self):
        if time.time() - self._last < 60:
            return
        self._last = time.time()
        todo = self._todo()
        if self.get("mode") == "each":
            for o in todo:
                if o["id"] in self.data["notified"]:
                    continue
                self.data["notified"][o["id"]] = time.time()
                self.notify(f"🧾 <b>Пробейте чек</b>: <code>{esc(self._line(o))}</code>\nПокупатель: {esc(o.get('buyer'))} · "
                            f"заказ #{esc(o['id'])}",
                            buttons=[[("✅ Чек пробит", f"duo:mod:receipts:act:done:{o['id']}", "finance"),
                                      ("🧾 Мой налог", "https://lknpd.nalog.ru/")]], kind="finance")
            self.save()
        else:
            today = datetime.now().strftime("%Y%m%d")
            if todo and datetime.now().strftime("%H:%M") >= str(self.get("time") or "21:00") and self.data["day"] != today:
                self.data["day"] = today
                self.save()
                total = sum(float(o.get("price") or 0) for o in todo)
                self.notify(f"🧾 <b>Чеки к пробитию</b>: {len(todo)} на {_rub(total)}\n\n"
                            + "\n".join(f"• <code>{esc(self._line(o))}</code>" for o in todo[:30]),
                            buttons=[[("✅ Все пробиты", "duo:mod:receipts:act:all", "finance"),
                                      ("🧾 Мой налог", "https://lknpd.nalog.ru/")]], kind="finance")

    def status(self) -> str:
        todo = self._todo()
        return f"не пробито: {len(todo)} на {_rub(sum(float(o.get('price') or 0) for o in todo))}"

    def actions(self) -> list:
        return [("all", "✅ Отметить все пробитыми")] if self._todo() else []

    def run_action(self, action, args) -> str:
        if action == "done" and args:
            self.data["done"][args[0]] = time.time()
        elif action == "all":
            for o in self._todo():
                self.data["done"][o["id"]] = time.time()
        else:
            return "Нет такого действия"
        self.save()
        return "Отмечено"


# ═════════════════════ резервная копия в Telegram ═════════════════════
class TgBackupModule(Module):
    KEY = "tgbackup"
    GROUP = "Безопасность"
    NAME = "AutoBackup"
    ICON = "💾"
    DESCRIPTION = ("Раз в день присылает владельцу архив с настройками, товарами и статистикой. Компьютер сломался — "
                   "восстановите всё из этого файла. В архиве ваши ключи: никому его не пересылайте.")
    DEFAULTS = {"time": "04:30"}
    FIELDS = [("time", "Во сколько присылать", "text", "например 04:30")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("day", "")
        self.data.setdefault("last", 0)

    def tick(self):
        today = datetime.now().strftime("%Y%m%d")
        if datetime.now().strftime("%H:%M") >= str(self.get("time") or "04:30") and self.data["day"] != today:
            self.data["day"] = today
            self.save()
            self._send()

    def _send(self) -> int:
        path = make_backup()
        n = send_file_to_owners(self.app, str(path), "💾 Бекап Castor. В нём ключи и пароли — не пересылайте никому. "
                                                     "Восстановление: распаковать в castor_data.")
        if n:
            self.data["last"] = time.time()
            self.save()
        return n

    def status(self) -> str:
        return ("последний: " + datetime.fromtimestamp(self.data["last"]).strftime("%d.%m %H:%M")) if self.data["last"] else "ещё не отправлялся"

    def actions(self) -> list:
        return [("now", "💾 Прислать бекап сейчас")]

    def run_action(self, action, args) -> str:
        if action == "now":
            return "Бекап отправлен в Telegram" if self._send() else "Не удалось: Telegram-бот не подключён"
        return "Нет такого действия"


# ═════════════════════ подозрительные покупатели ═════════════════════
class RiskModule(Module):
    KEY = "risk"
    GROUP = "Безопасность"
    ORDER = 1
    NAME = "BuyerCheck"
    ICON = "🕵️"
    DESCRIPTION = ("Первый заказ от нового покупателя на крупную сумму — частый сценарий обмана. Бот предупредит вас "
                   "и, если нужно, придержит выдачу до вашего «Выдать».")
    DEFAULTS = {"min_price": 1500, "mode": "warn", "only_first": True,
                "text_hold": "Спасибо за заказ! Проверяю его и скоро выдам 🙌"}
    FIELDS = [("min_price", "Заказ от, ₽", "number"), ("only_first", "Только первая покупка у вас", "bool"),
              ("mode", "Что делать", "select", [["warn", "только предупредить меня"], ["hold", "придержать выдачу до моего решения"]]),
              ("text_hold", "Сообщение покупателю, пока заказ придержан", "area")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("held", {})
        self.data.setdefault("flagged", 0)

    def on_order(self, o) -> bool:
        if float(o.price or 0) < float(self.get("min_price") or 0):
            return False
        cust = self.app.customers.get(o.platform, o.buyer) or {}
        if self.get("only_first", True) and int(cust.get("orders", 0)) > 1:
            return False
        self.data["flagged"] += 1
        head = (f"🕵️ <b>Проверьте покупателя</b>: {esc(o.buyer)} — {'первая покупка, ' if int(cust.get('orders', 0)) <= 1 else ''}"
                f"{o.price} {esc(getattr(o, 'currency', '₽'))}\n📦 {esc(o.title)} · заказ <code>{esc(o.id)}</code>")
        prof = (f"https://funpay.com/users/{o.buyer_id}/" if o.platform == "funpay" and o.buyer_id else None)
        if self.get("mode") == "hold":
            self.data["held"][str(o.id)] = dict(order_to_dict(o), t=time.time())
            self.save()
            o.reply(self.get("text_hold"))
            row = [("✅ Выдать", f"duo:mod:risk:act:ok:{o.id}", "delivery"), ("🚫 Не выдавать", f"duo:mod:risk:act:no:{o.id}", "delivery")]
            btns = [row] + ([[("👤 Профиль покупателя", prof)]] if prof else [])
            self.notify(head + "\n\nВыдача придержана — решите кнопкой.", buttons=btns, kind="fp_order")
            return True
        self.save()
        self.notify(head + "\n\nВыдача идёт как обычно.", buttons=[[("👤 Профиль покупателя", prof)]] if prof else None,
                    kind="fp_order")
        return False

    def status(self) -> str:
        return f"проверено: {self.data['flagged']}" + (f" · ждут решения: {len(self.data['held'])}" if self.data["held"] else "")

    def actions(self) -> list:
        return [(f"ok:{oid}", f"✅ Выдать #{oid}") for oid in list(self.data["held"])[:6]]

    def run_action(self, action, args) -> str:
        if not args:
            return "Нет заказа"
        d = self.data["held"].pop(args[0], None)
        self.save()
        if not d:
            return "Заказ уже обработан"
        if action == "ok":
            continue_order(self.app, order_from_dict(self.app, d), skip=(self.KEY,))
            return "Выдаю заказ"
        if action == "no":
            return "Заказ не выдан. Если нужно — сделайте возврат в карточке заказа"
        return "Нет такого действия"
