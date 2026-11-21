"""Просроченные заказы: если покупатель долго не подтверждает выданный заказ, бот готовит обращение
в поддержку FunPay — с номером заказа и готовым текстом — и присылает вам кнопкой.

Обращение отправляется вами в один клик: у поддержки FunPay нет открытого способа создавать тикеты
программно, а неофициальная автоматизация может привести к блокировке аккаунта.
"""
from __future__ import annotations

import time

from ..core import esc, fmt_template
from . import Module


class OverdueModule(Module):
    KEY = "overdue"
    GROUP = "Заказы"
    ORDER = 50
    NAME = "Просроченные заказы"
    ICON = "🎫"
    DESCRIPTION = "Покупатель долго не подтверждает заказ — бот готовит обращение в поддержку FunPay и присылает его вам."
    PLATFORMS = ("funpay",)
    DEFAULTS = {
        "after_hours": 72,
        "only_delivered": True,
        "remind_buyer": True,
        "text_buyer": "Здравствуйте, $username! Если всё получили — подтвердите, пожалуйста, заказ #$order_id 🙏",
        "text_ticket": ("Здравствуйте! Заказ #$order_id выполнен, товар выдан покупателю $username, "
                        "но заказ не подтверждён уже $hours ч. Прошу подтвердить заказ. Спасибо!"),
    }
    FIELDS = [
        ("after_hours", "Считать просроченным через, ч", "number"),
        ("only_delivered", "Только выданные заказы", "bool"),
        ("remind_buyer", "Сначала напомнить покупателю", "bool"),
        ("text_buyer", "Напоминание покупателю", "area", "$username $order_id"),
        ("text_ticket", "Текст обращения в поддержку", "area", "$username $order_id $hours $title"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("done", {})
        self._last = 0

    def tick(self):
        if time.time() - self._last < 300:
            return
        self._last = time.time()
        limit = float(self.get("after_hours", 72) or 72) * 3600
        for key, o in list(self.app.state.data.get("open_orders", {}).items()):
            if o.get("platform") != "funpay" or key in self.data["done"]:
                continue
            age = time.time() - o.get("t", time.time())
            if age < limit:
                continue
            st = self.app.stats.find(o.get("id"), "funpay") or {}
            if st.get("refunded") or (self.get("only_delivered", True) and not st.get("delivered")):
                continue
            hours = int(age // 3600)
            v = dict(username=o.get("buyer", ""), order_id=o.get("id"), hours=hours, title=o.get("title", ""))
            if self.get("remind_buyer", True):
                self.reply("funpay", o.get("chat_id"), fmt_template(self.get("text_buyer"), **v), o.get("account"), o.get("buyer"))
            ticket = fmt_template(self.get("text_ticket"), **v)
            self.notify(f"🎫 <b>Заказ не подтверждён {hours} ч</b>: <code>#{esc(o.get('id'))}</code> · {esc(o.get('buyer'))}\n"
                        f"📦 {esc(o.get('title', ''))[:80]}\n\nТекст для поддержки (нажмите, чтобы скопировать):\n"
                        f"<code>{esc(ticket)}</code>",
                        buttons=[[("🎫 Открыть поддержку", "https://support.funpay.com/"),
                                  ("🔗 Заказ", f"https://funpay.com/orders/{o.get('id')}/")]], kind="reminder")
            self.data["done"][key] = time.time()
            self.save()
        # чистка
        for k, t in list(self.data["done"].items()):
            if time.time() - t > 30 * 86400:
                del self.data["done"][k]

    def status(self) -> str:
        n = sum(1 for k, o in self.app.state.data.get("open_orders", {}).items() if o.get("platform") == "funpay")
        return f"неподтверждённых заказов FunPay: {n}"
