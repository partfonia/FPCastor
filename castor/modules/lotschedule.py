"""Расписание лотов: в нерабочее время бот выключает все активные лоты аккаунта FunPay,
а утром включает обратно ровно те, что были включены.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from ..core import esc, log
from . import Module

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def in_window(frm: str, to: str, days: list, now: datetime = None) -> bool:
    now = now or datetime.now()
    if days and DAYS[now.weekday()] not in [d.strip().lower()[:2] for d in days]:
        return False
    t = now.strftime("%H:%M")
    return frm <= t < to if frm <= to else (t >= frm or t < to)


class LotScheduleModule(Module):
    KEY = "lotschedule"
    GROUP = "Лоты и цены"
    ORDER = 50
    NAME = "Расписание лотов"
    ICON = "🕰"
    DESCRIPTION = "Выключает лоты FunPay на ночь (или в выбранные дни) и включает их обратно по расписанию."
    PLATFORMS = ("funpay",)
    DEFAULTS = {"rules": []}   # [{"account", "from": "09:00", "to": "23:00", "days": [], "enabled": True}]
    FIELDS = [("rules", "Расписания", "items")]
    ITEM_FIELDS = {
        "rules": [("account", "Аккаунт (ID, пусто — все)", "text"),
                  ("from", "Лоты включены с", "text", "например 09:00"), ("to", "до", "text", "например 23:00"),
                  ("days", "Дни (пусто — каждый день)", "lines", "пн вт ср чт пт сб вс — по одному в строке"),
                  ("enabled", "Включено", "bool")],
    }

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("off", {})    # account_id → [id лотов, которые мы выключили]
        self._busy = False
        self._last = 0

    def tick(self):
        if self._busy or time.time() - self._last < 60:
            return
        self._last = time.time()
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _accounts(self, rule):
        if rule.get("account"):
            svc = self.app.service("funpay", rule["account"])
            return [svc] if svc else []
        return list(self.app.fp.values())

    def _run(self):
        try:
            for rule in self.get("rules", []):
                if rule.get("enabled", True) is False:
                    continue
                want_on = in_window(rule.get("from", "00:00"), rule.get("to", "23:59"), rule.get("days") or [])
                for svc in self._accounts(rule):
                    if not svc or not svc.ok:
                        continue
                    acc = svc.acc_id
                    off = self.data["off"].get(acc)
                    if not want_on and off is None:
                        self._switch_off(svc)
                    elif want_on and off is not None:
                        self._switch_on(svc, off)
        finally:
            self._busy = False

    def _switch_off(self, svc):
        lots = [x for x in svc.client.get_my_lots() if x["active"]]
        done = []
        for lot in lots:
            try:
                svc.client.set_lot_active(lot["id"], False)
                done.append(lot["id"])
            except Exception as e:
                log.warning("🕰 Не выключился лот %s: %s", lot["id"], e)
            time.sleep(1.5)
        self.data["off"][svc.acc_id] = done
        self.save()
        self.notify(f"🕰 Расписание: выключил {len(done)} лот(ов) на {esc(self.app.account_name(svc.acc_id))}", kind="raise")

    def _switch_on(self, svc, ids):
        n = 0
        for lid in ids:
            try:
                svc.client.set_lot_active(lid, True)
                n += 1
            except Exception as e:
                log.warning("🕰 Не включился лот %s: %s", lid, e)
            time.sleep(1.5)
        self.data["off"].pop(svc.acc_id, None)
        self.save()
        self.notify(f"🕰 Расписание: включил обратно {n} лот(ов) на {esc(self.app.account_name(svc.acc_id))}", kind="raise")

    def status(self) -> str:
        off = sum(len(v) for v in self.data["off"].values())
        return f"расписаний: {len(self.get('rules', []))}" + (f" · сейчас выключено лотов: {off}" if off else "")

    def actions(self) -> list:
        return [("restore", "↩️ Включить все выключенные сейчас")] if self.data["off"] else []

    def run_action(self, action, args) -> str:
        if action == "restore":
            def work():
                for acc, ids in list(self.data["off"].items()):
                    svc = self.app.service("funpay", acc)
                    if svc and svc.ok:
                        self._switch_on(svc, ids)
            threading.Thread(target=work, daemon=True).start()
            return "Включаю лоты обратно"
        return "Нет такого действия"
