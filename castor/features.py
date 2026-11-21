"""Дополнительные функции: планировщик (отчёты, напоминания, бекапы), антифлуд, вебхуки, остатки."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import TYPE_CHECKING

import requests

from .core import Products, esc, fmt_template, log, make_backup, prune_backups

if TYPE_CHECKING:
    from .app import App


class AntiFlood:
    """Не даёт боту отвечать в один чат чаще N раз в минуту (защита от зацикливания и спама)."""

    def __init__(self, app: "App"):
        self.app = app
        self.hits: dict = defaultdict(lambda: deque(maxlen=50))

    def allow(self, platform, chat_id) -> bool:
        f = self.app.cfg.get("features.anti_flood", {})
        if not f.get("enabled"):
            return True
        q = self.hits[f"{platform}:{chat_id}"]
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= int(f.get("max_replies_per_min", 4)):
            return False
        q.append(now)
        return True


class Webhooks:
    def __init__(self, app: "App"):
        self.app = app

    def emit(self, event: str, text: str, data: dict):
        w = self.app.cfg.get("features.webhooks", {})
        if not w.get("enabled") or event not in w.get("events", []):
            return
        for url in w.get("urls", []):
            threading.Thread(target=self._post, args=(url, event, text, data), daemon=True).start()

    @staticmethod
    def _post(url, event, text, data):
        try:
            if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
                payload = {"content": text[:1900]}
            else:
                payload = {"event": event, "text": text, "data": data, "time": time.time()}
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            log.debug("webhook %s: %s", url, e)


class LowStock:
    def __init__(self, app: "App"):
        self.app = app

    def check(self, file: str):
        f = self.app.cfg.get("features.low_stock", {})
        if not f.get("enabled") or not file:
            return
        left = Products.count(file)
        key = f"lowstock:{file}"
        thr = int(f.get("threshold", 3))
        if left <= thr:
            if not self.app.state.get("flags", key):
                self.app.state.mark("flags", key)
                self.app.notify("low_stock", f"📉 Заканчивается товар в <code>{esc(file)}</code>: осталось <b>{left}</b>",
                                buttons=[[("📦 Автовыдача", "duo:dlv")]])
        elif self.app.state.get("flags", key):
            self.app.state.pop("flags", key)


class Scheduler:
    def __init__(self, app: "App"):
        self.app = app

    def start(self):
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def _once_per_day(self, key) -> bool:
        today = datetime.now().strftime("%Y%m%d")
        if self.app.state.get("flags", key) == today:
            return False
        self.app.state.mark("flags", key, today)
        return True

    def _loop(self):
        while self.app.running:
            try:
                self.tick()
            except Exception as e:
                log.warning("Планировщик: %s", e)
            time.sleep(30)

    def tick(self):
        cfg = self.app.cfg
        try:
            self.app.withdraw.tick()
        except Exception as e:
            log.warning("Автовывод: %s", e)
        now = datetime.now()
        # отчёт за день
        rep = cfg.get("features.daily_report", {})
        if rep.get("enabled") and now.strftime("%H:%M") >= rep.get("time", "23:55") and self._once_per_day("report"):
            self.app.notify("report", self.report_text())
        # бекап
        b = cfg.get("features.auto_backup", {})
        if b.get("enabled") and now.hour >= 4 and self._once_per_day("backup"):
            path = make_backup()
            prune_backups(int(b.get("keep", 7)))
            log.info("💾 Автобекап: %s", path.name)
        # напоминания подтвердить заказ
        r = cfg.get("features.confirm_reminder", {})
        if r.get("enabled"):
            limit = float(r.get("after_hours", 24)) * 3600
            for key, o in list(self.app.state.data.get("open_orders", {}).items()):
                if time.time() - o.get("t", 0) < limit or self.app.state.get("reminded", key):
                    continue
                self.app.state.mark("reminded", key)
                text = fmt_template(r.get("text"), username=o.get("buyer"), order_id=o.get("id"),
                                    order_title=o.get("title"))
                if self.app.send(o["platform"], o["chat_id"], text, o.get("buyer"), o.get("account")):
                    self.app.notify("reminder", f"⏰ Напомнил {esc(o.get('buyer'))} подтвердить заказ "
                                                f"<code>{esc(o.get('id'))}</code>")
        # отпуск закончился
        v = cfg.get("features.vacation", {})
        if v.get("enabled") and v.get("until"):
            try:
                if now >= datetime.strptime(v["until"], "%Y-%m-%d"):
                    cfg.set("features.vacation.enabled", False)
                    self.app.notify("start", "🏖 Режим отпуска выключен автоматически")
            except ValueError:
                pass

    def report_text(self) -> str:
        s = self.app.stats
        t = s.today()
        total = t["total"]["sum"]
        prods = Products.list()
        low = [f"{esc(k)}: {v}" for k, v in prods.items() if v <= int(self.app.cfg.get("features.low_stock.threshold", 3))]
        return (f"🌙 <b>Итоги дня {datetime.now():%d.%m}</b>\n\n"
                f"🔵 FunPay: {t['funpay']['count']} заказов · {t['funpay']['sum']}\n"
                f"🟢 Playerok: {t['playerok']['count']} заказов · {t['playerok']['sum']}\n"
                f"💰 Выручка: <b>{round(total, 2)}</b>\n"
                f"📈 Прибыль: <b>{t['total']['profit']}</b>"
                + (f" (расходы {t['total']['expenses']})" if t["total"]["expenses"] else "")
                + (f"\n⚠️ Без себестоимости: {t['funpay']['no_cost'] + t['playerok']['no_cost']} зак." if
                   t['funpay']['no_cost'] + t['playerok']['no_cost'] else "") + "\n"
                f"📦 Автовыдач всего: {s.data.get('deliveries', 0)}"
                + ("\n\n📉 Мало товара:\n" + "\n".join(low) if low else ""))
