"""Модули лотов FunPay: шаблоны (много лотов из таблицы), массовая смена цен, слежка за конкурентами."""
from __future__ import annotations

import math
import re
import threading
import time

from ..core import esc, log
from . import Module


def _svc(app, acc):
    svc = app.service("funpay", acc or None)
    return svc if svc and svc.ok else None


# ═════════════════════ шаблоны лотов ═════════════════════
class LotTemplatesModule(Module):
    KEY = "lottemplates"
    GROUP = "Лоты и цены"
    NAME = "Лоты из таблицы"
    ICON = "🗂"
    DESCRIPTION = ("Создаёт сразу много лотов по образцу: берёт ваш готовый лот и делает копии с другими названиями, "
                   "ценами и количеством из таблицы.")
    PLATFORMS = ("funpay",)
    DEFAULTS = {"account": "", "template_lot": "", "rows": [], "desc_suffix": ""}
    FIELDS = [("account", "Аккаунт (ID, пусто — первый)", "text"),
              ("template_lot", "ID лота-образца", "text", "все поля категории возьмутся из него"),
              ("rows", "Таблица: название | цена | количество", "lines",
               "по строке на лот, например: Ключ Elden Ring | 1490 | 5. Цена — «вам на баланс»"),
              ("desc_suffix", "Добавить в конец описания (необязательно)", "area")]

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self.data.setdefault("last", "")

    def status(self) -> str:
        return "создаю…" if self._busy else (self.data.get("last") or f"строк в таблице: {len(self.get('rows', []))}")

    def actions(self) -> list:
        return [("run", "▶️ Создать лоты из таблицы")]

    def run_action(self, action, args) -> str:
        if action != "run":
            return "Нет такого действия"
        if self._busy:
            return "Уже создаю"
        svc = _svc(self.app, self.get("account"))
        if not svc:
            return "Аккаунт FunPay не подключён"
        if not str(self.get("template_lot", "")).strip():
            return "Укажите ID лота-образца"
        rows = [r for r in self.get("rows", []) if str(r).strip()]
        if not rows:
            return "Таблица пуста"
        self._busy = True
        threading.Thread(target=self._run, args=(svc, rows), daemon=True).start()
        return f"Создаю {len(rows)} лот(ов) — пришлю отчёт"

    def _run(self, svc, rows):
        ok, bad = 0, []
        try:
            base = svc.client.get_lot_form(str(self.get("template_lot")).strip())
            for row in rows:
                parts = [x.strip() for x in str(row).split("|")]
                title = parts[0]
                try:
                    price = float(parts[1].replace(",", ".")) if len(parts) > 1 and parts[1] else None
                    amount = int(float(parts[2])) if len(parts) > 2 and parts[2] else None
                except ValueError:
                    bad.append(f"{title}: цена или количество не число")
                    continue
                fields = []
                for k, v in base:
                    if k == "offer_id":
                        v = "0"
                    elif re.fullmatch(r"fields\[summary\]\[\w+\]", k):
                        v = title
                    elif re.fullmatch(r"fields\[desc\]\[\w+\]", k) and self.get("desc_suffix"):
                        v = (v or "") + "\n\n" + self.get("desc_suffix")
                    elif k == "price" and price is not None:
                        v = f"{price:g}"
                    elif k == "amount" and amount is not None:
                        v = str(amount)
                    fields.append((k, v))
                try:
                    svc.client._save_form(fields)
                    ok += 1
                except Exception as e:
                    bad.append(f"{title}: {e}")
                time.sleep(2)
        except Exception as e:
            bad.append(f"образец: {e}")
        finally:
            self._busy = False
        self.data["last"] = f"последний раз: создано {ok}, ошибок {len(bad)}"
        self.save()
        self.notify(f"🗂 <b>Лоты из таблицы</b>: создано {ok}" +
                    ("\n\nНе получилось:\n" + "\n".join(f"• {esc(x)}" for x in bad[:10]) if bad else ""), kind="start")


# ═════════════════════ массовая смена цен ═════════════════════
class BulkPriceModule(Module):
    KEY = "bulkprice"
    GROUP = "Лоты и цены"
    NAME = "Массовая смена цен"
    ICON = "🏷"
    DESCRIPTION = "Меняет цены сразу у многих лотов: на процент, на сумму или до ровного числа. Сначала покажет, что изменится."
    PLATFORMS = ("funpay",)
    DEFAULTS = {"account": "", "nodes": [], "mode": "percent", "value": 10, "round_to": 1, "min_price": 1}
    FIELDS = [("account", "Аккаунт (ID, пусто — первый)", "text"),
              ("nodes", "Только эти разделы (ID по строке)", "lines", "пусто — все лоты аккаунта"),
              ("mode", "Как менять", "select", [["percent", "на процент (+10 или -5)"], ["add", "на сумму в ₽ (+50 или -20)"],
                                                 ["round", "только округлить"]]),
              ("value", "Значение", "number"), ("round_to", "Округлять до, ₽", "number", "1 — до рубля, 10 — до десятков"),
              ("min_price", "Не ниже, ₽", "number")]

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self.data.setdefault("last", "")

    def _new(self, cur: float) -> float:
        mode, val = self.get("mode", "percent"), float(self.get("value") or 0)
        new = cur * (1 + val / 100) if mode == "percent" else cur + val if mode == "add" else cur
        step = float(self.get("round_to") or 0)
        if step > 0:
            new = math.floor(new / step + 0.5) * step
        return round(max(new, float(self.get("min_price") or 0)), 2)

    def status(self) -> str:
        return "работаю…" if self._busy else (self.data.get("last") or "готов")

    def actions(self) -> list:
        return [("preview", "👀 Показать, что изменится"), ("apply", "✅ Применить")]

    def run_action(self, action, args) -> str:
        if self._busy:
            return "Уже работаю — дождитесь окончания"
        svc = _svc(self.app, self.get("account"))
        if not svc:
            return "Аккаунт FunPay не подключён"
        if action not in ("preview", "apply"):
            return "Нет такого действия"
        self._busy = True
        threading.Thread(target=self._run, args=(svc, action == "apply"), daemon=True).start()
        return "Считаю — пришлю список" if action == "preview" else "Меняю цены — пришлю отчёт"

    def _run(self, svc, apply):
        lines, changed, errors = [], 0, 0
        try:
            only = {str(n).strip() for n in self.get("nodes", []) if str(n).strip()}
            lots = [x for x in svc.client.get_my_lots() if not only or str(x["node"]) in only]
            for lot in lots:
                try:
                    cur = svc.client.get_lot_price(lot["id"])
                    if cur is None:
                        continue
                    new = self._new(cur)
                    if abs(new - cur) < 0.01:
                        continue
                    lines.append(f"• {lot['title'][:40]}: {cur:g} → {new:g} ₽")
                    if apply:
                        svc.client.set_lot_price(lot["id"], new)
                        changed += 1
                        time.sleep(1.5)
                except Exception as e:
                    errors += 1
                    log.warning("🏷 Лот %s: %s", lot["id"], e)
                time.sleep(0.4)
        finally:
            self._busy = False
        head = (f"🏷 <b>Цены изменены</b>: {changed} лот(ов)" if apply else
                f"🏷 <b>Предпросмотр</b>: изменится {len(lines)} лот(ов). Чтобы применить — «✅ Применить» в модуле.")
        self.data["last"] = f"последний раз: {'изменено' if apply else 'к изменению'} {changed if apply else len(lines)}"
        self.save()
        self.notify(head + (f", ошибок {errors}" if errors else "") + ("\n\n" + esc("\n".join(lines[:40])) if lines else ""),
                    kind="start")


# ═════════════════════ слежка за конкурентами ═════════════════════
class WatchModule(Module):
    KEY = "watch"
    GROUP = "Лоты и цены"
    NAME = "Слежка за конкурентами"
    ICON = "🔭"
    DESCRIPTION = "Следит за разделами FunPay: сообщает, когда появился новый продавец или кто-то сильно снизил цену."
    PLATFORMS = ("funpay",)
    DEFAULTS = {"nodes": [], "interval_min": 30, "drop_percent": 10, "new_sellers": True}
    FIELDS = [("nodes", "Разделы", "items"), ("interval_min", "Проверять каждые, мин", "number"),
              ("drop_percent", "Сообщать, если минимальная цена упала на, %", "number"),
              ("new_sellers", "Сообщать о новых продавцах", "bool")]
    ITEM_FIELDS = {"nodes": [("node", "ID раздела", "text", "число из ссылки funpay.com/lots/…/"),
                             ("name", "Название для себя", "text"), ("filter", "Только лоты со словом", "text"),
                             ("enabled", "Включено", "bool")]}

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("snap", {})
        self._next, self._busy = 0, False

    def tick(self):
        if self._busy or time.time() < self._next:
            return
        self._next = time.time() + max(5.0, float(self.get("interval_min", 30) or 30)) * 60
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            svc = _svc(self.app, None)
            if not svc:
                return
            me = (svc.client.username or "").lower()
            for n in self.get("nodes", []):
                if n.get("enabled", True) is False or not str(n.get("node", "")).strip():
                    continue
                node = str(n["node"]).strip()
                try:
                    offers = svc.client.get_public_offers(node)
                except Exception as e:
                    log.warning("🔭 Раздел %s: %s", node, e)
                    continue
                flt = str(n.get("filter") or "").lower()
                offers = [o for o in offers if o["seller"].lower() != me and (not flt or flt in o["title"].lower())]
                if not offers:
                    continue
                sellers = sorted({o["seller"] for o in offers})
                low = min(o["price"] for o in offers)
                prev = self.data["snap"].get(node)
                name = n.get("name") or f"раздел {node}"
                msgs = []
                if prev:
                    new = [s for s in sellers if s not in prev["sellers"]]
                    if new and self.get("new_sellers", True):
                        msgs.append("новые продавцы: " + ", ".join(new[:8]))
                    drop = float(self.get("drop_percent", 10) or 10)
                    if prev["low"] and low < prev["low"] * (1 - drop / 100):
                        who = next(o["seller"] for o in offers if o["price"] == low)
                        msgs.append(f"минимальная цена {prev['low']:g} → {low:g} ₽ ({who})")
                self.data["snap"][node] = {"sellers": sellers, "low": low, "t": time.time(), "count": len(offers)}
                if msgs:
                    self.notify(f"🔭 <b>{esc(name)}</b>: " + "; ".join(esc(x) for x in msgs),
                                buttons=[[("🔗 Открыть раздел", f"https://funpay.com/lots/{node}/")]], kind="raise")
                time.sleep(2)
            self.save()
        finally:
            self._busy = False

    def status(self) -> str:
        parts = [f"{(next((n.get('name') for n in self.get('nodes', []) if str(n.get('node')) == k), None) or k)}: "
                 f"от {v['low']:g} ₽, продавцов {len(v['sellers'])}" for k, v in self.data["snap"].items()]
        return "; ".join(parts[:3]) or f"разделов: {len(self.get('nodes', []))}"

    def actions(self) -> list:
        return [("now", "▶️ Проверить сейчас")]

    def run_action(self, action, args) -> str:
        if action == "now":
            self._next = 0
            self.tick()
            return "Проверяю разделы"
        return "Нет такого действия"
