"""Автоцена: держит цену выбранных лотов FunPay чуть ниже самого дешёвого конкурента — в ваших пределах.

На странице раздела FunPay показывает цену для покупателя (с комиссией), а в форме лота — цену «вам».
Бот сам считает это соотношение по вашему лоту, поэтому пределы «от / до» вы задаёте в цене «вам».
"""
from __future__ import annotations

import threading
import time

from ..core import esc, log
from . import Module


class AutoPriceModule(Module):
    KEY = "autoprice"
    GROUP = "Лоты и цены"
    ORDER = 50
    NAME = "Автоцена"
    ICON = "💹"
    DESCRIPTION = "Подстраивает цену лотов под конкурентов: на шаг дешевле самого дешёвого, но не ниже вашего минимума."
    PLATFORMS = ("funpay",)
    DEFAULTS = {
        "interval_min": 15,
        "only_online": True,
        "lots": [],   # [{"lot_id", "node", "min", "max", "step", "account", "enabled"}]
    }
    FIELDS = [
        ("lots", "Лоты под автоцену", "items"),
        ("interval_min", "Проверять каждые, мин", "number"),
        ("only_online", "Сравнивать только с продавцами онлайн", "bool"),
    ]
    ITEM_FIELDS = {
        "lots": [("lot_id", "ID лота", "text", "число из ссылки offer?id=…"),
                 ("node", "ID раздела", "text", "число из ссылки funpay.com/lots/…/"),
                 ("min", "Минимальная цена «вам», ₽", "number"), ("max", "Максимальная цена «вам», ₽", "number"),
                 ("step", "На сколько дешевле конкурента, ₽", "number"),
                 ("account", "Аккаунт (пусто — первый)", "text"), ("enabled", "Включено", "bool")],
    }

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("last", {})   # lot_id → {"t", "price", "note"}
        self._next = 0
        self._busy = False

    def tick(self):
        if self._busy or time.time() < self._next:
            return
        self._next = time.time() + max(5.0, float(self.get("interval_min", 15) or 15)) * 60
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            for lot in self.get("lots", []):
                if lot.get("enabled", True) is False or not lot.get("lot_id") or not lot.get("node"):
                    continue
                try:
                    note = self.adjust(lot)
                except Exception as e:
                    note = f"ошибка: {e}"
                    log.warning("💹 Автоцена %s: %s", lot.get("lot_id"), e)
                self.data["last"][str(lot["lot_id"])] = {"t": time.time(), "note": note}
                self.save()
                time.sleep(3)
        finally:
            self._busy = False

    def adjust(self, lot) -> str:
        svc = self.app.service("funpay", lot.get("account") or None)
        if not svc or not svc.ok:
            return "аккаунт FunPay не подключён"
        c = svc.client
        lid = str(lot["lot_id"])
        offers = c.get_public_offers(lot["node"])
        own = next((x for x in offers if x["id"] == lid), None)
        cur = c.get_lot_price(lid)
        if cur is None:
            return "не удалось прочитать цену лота"
        if not own:
            return "лот не виден в разделе (выключен?) — цену не трогаю"
        k = own["price"] / cur if cur else 1.0          # цена покупателя / цена «вам»
        me = (getattr(c, "username", "") or "").lower()
        comps = [x for x in offers if x["id"] != lid and x["seller"].lower() != me
                 and (x["online"] or not self.get("only_online", True))]
        lo, hi = float(lot.get("min") or 0), float(lot.get("max") or 0) or float("inf")
        if not comps:
            target = hi if hi != float("inf") else cur
            why = "конкурентов нет"
        else:
            best = min(x["price"] for x in comps)
            target = (best - float(lot.get("step") or 1)) / k
            why = f"дешевле всех: {best:g} ₽ у {comps[[x['price'] for x in comps].index(best)]['seller']}"
        target = round(min(max(target, lo), hi), 2)
        if abs(target - cur) < 0.01:
            return f"цена {cur:g} ₽ в порядке ({why})"
        c.set_lot_price(lid, target)
        log.info("💹 Автоцена: лот %s %g → %g ₽ (%s)", lid, cur, target, why)
        self.notify(f"💹 Автоцена: лот <code>{esc(lid)}</code> {cur:g} → <b>{target:g} ₽</b>\n<i>{esc(why)}</i>", kind="raise")
        return f"{cur:g} → {target:g} ₽ ({why})"

    def status(self) -> str:
        n = len([x for x in self.get("lots", []) if x.get("enabled", True) is not False])
        return f"лотов: {n}" + (" · проверяю…" if self._busy else "")

    def actions(self) -> list:
        return [("now", "▶️ Проверить цены сейчас"), ("report", "📋 Последние изменения")]

    def run_action(self, action, args) -> str:
        if action == "now":
            self._next = 0
            self.tick()
            return "Проверяю цены — результат появится в «Последние изменения» через минуту"
        if action == "report":
            if not self.data["last"]:
                return "Ещё не проверял"
            return "\n".join(f"• {lid}: {v.get('note')}" for lid, v in self.data["last"].items())
        return "Нет такого действия"
