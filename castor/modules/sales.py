"""Модули продаж: автовосстановление лотов, предзаказ, выдача по шагам, наборы, бонус постоянным."""
from __future__ import annotations

import re
import threading
import time

from ..core import Products, esc, fmt_template, log
from . import Module, continue_order, order_from_dict, order_to_dict


# ═════════════════════ автовосстановление лотов ═════════════════════
class AutoRestoreModule(Module):
    KEY = "autorestore"
    GROUP = "Лоты и цены"
    NAME = "AutoRestore"
    ICON = "♻️"
    DESCRIPTION = ("FunPay сам выключает лот, когда он продан или кончилось количество. Бот включает его обратно — "
                   "кроме лотов, которые скрыты из-за пустого товара или по расписанию.")
    PLATFORMS = ("funpay",)
    DEFAULTS = {"interval_min": 10, "only_with_stock": True, "exclude": []}
    FIELDS = [
        ("interval_min", "Проверять каждые, мин", "number"),
        ("only_with_stock", "Не включать, если в файле автовыдачи нет товара", "bool"),
        ("exclude", "Не трогать эти лоты (ID, по одному в строке)", "lines"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("known", {})   # account → [id активных лотов]
        self.data.setdefault("restored", 0)
        self._next, self._busy = 0, False

    def tick(self):
        if self._busy or time.time() < self._next:
            return
        self._next = time.time() + max(3.0, float(self.get("interval_min", 10) or 10)) * 60
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _file_empty(self, acc, lid) -> bool:
        for r in self.app.cfg.get("delivery", []):
            if r.get("mode", "file") != "file":
                continue
            for lot in r.get("lots", []):
                if str(lot.get("id")) == str(lid) and (not lot.get("account") or lot.get("account") == acc):
                    return Products.count(r.get("file", "")) == 0
        return False

    def _run(self):
        try:
            exclude = {str(x).strip() for x in self.get("exclude", [])}
            hidden = set(self.app.state.data.get("hidden_lots", {}).keys())
            sched = self.app.modules.get("lotschedule")
            sched_off = {f"{a}:{i}" for a, ids in (sched.data.get("off", {}) if sched else {}).items() for i in ids}
            for acc, svc in list(self.app.fp.items()):
                if not svc.ok:
                    continue
                lots = svc.client.get_my_lots()
                active = {x["id"] for x in lots if x["active"]}
                titles = {x["id"]: x["title"] for x in lots}
                prev = set(self.data["known"].get(acc, []))
                back = []
                for lid in prev - active:
                    key = f"{acc}:{lid}"
                    if lid in exclude or key in hidden or key in sched_off or lid not in titles:
                        continue
                    if self.get("only_with_stock", True) and self._file_empty(acc, lid):
                        continue
                    try:
                        svc.client.set_lot_active(lid, True)
                        back.append(titles.get(lid, lid))
                        active.add(lid)
                    except Exception as e:
                        log.warning("♻️ Лот %s не включился: %s", lid, e)
                    time.sleep(1.5)
                self.data["known"][acc] = sorted(active)
                if back:
                    self.data["restored"] += len(back)
                    self.notify(f"♻️ Включил обратно {len(back)} лот(ов){self.app.acc_tag(acc)}:\n"
                                + "\n".join(f"• {esc(t)[:60]}" for t in back[:10]), kind="raise")
            self.save()
        finally:
            self._busy = False

    def status(self) -> str:
        return f"восстановлено лотов: {self.data['restored']}"


# ═════════════════════ предзаказ ═════════════════════
class PreorderModule(Module):
    KEY = "preorder"
    GROUP = "Выдача"
    ORDER = 60
    NAME = "PreOrder"
    ICON = "⏳"
    DESCRIPTION = ("Товар закончился, а заказ пришёл — бот не делает возврат, а ставит покупателя в очередь "
                   "и выдаёт сам, как только вы пополните файл.")
    DEFAULTS = {"text_wait": "Товар уже в пути 🙌 Выдам автоматически, как только он появится. Обычно это недолго.",
                "max_hours": 24}
    FIELDS = [("text_wait", "Сообщение покупателю", "area", "$username $order_id"),
              ("max_hours", "Напомнить мне, если ждёт дольше, ч", "number")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("queue", [])

    def on_order(self, o) -> bool:
        rule = self.app.find_rule(o)
        if not rule or rule.get("mode", "file") != "file":
            return False
        if Products.count(rule.get("file", "")) >= self.app._units(o, rule):
            return False
        with self.lock:
            self.data["queue"].append(dict(order_to_dict(o), t=time.time(), file=rule.get("file", ""), warned=False))
            self.save()
        o.reply(fmt_template(self.get("text_wait"), username=o.buyer, order_id=o.id))
        self.notify(f"⏳ <b>Предзаказ</b> <code>{esc(o.id)}</code>: нет товара в <code>{esc(rule.get('file'))}</code>. "
                    f"Пополните файл — бот выдаст сам.")
        return True

    def tick(self):
        with self.lock:
            for item in list(self.data["queue"]):
                o = order_from_dict(self.app, item)
                rule = self.app.find_rule(o)
                if not rule:
                    self.data["queue"].remove(item)
                    continue
                if Products.count(rule.get("file", "")) >= self.app._units(o, rule):
                    if self.app.deliver(o, rule):
                        self.data["queue"].remove(item)
                        self.notify(f"⏳ Предзаказ <code>{esc(o.id)}</code> выдан")
                elif not item.get("warned") and time.time() - item["t"] > float(self.get("max_hours", 24)) * 3600:
                    item["warned"] = True
                    self.notify(f"⏳ Заказ <code>{esc(o.id)}</code> ждёт товар больше {self.get('max_hours')} ч — "
                                f"пополните <code>{esc(item.get('file'))}</code> или сделайте возврат")
            self.save()

    def status(self) -> str:
        return f"в очереди: {len(self.data['queue'])}"

    def actions(self) -> list:
        return [("list", "📋 Очередь")] if self.data["queue"] else []

    def run_action(self, action, args) -> str:
        if action == "list":
            return "\n".join(f"• #{x['id']} {x['buyer']} — {x['title'][:40]} (файл {x['file']})" for x in self.data["queue"]) or "Очередь пуста"
        return "Нет такого действия"


# ═════════════════════ выдача по шагам ═════════════════════
class StepsModule(Module):
    KEY = "steps"
    GROUP = "Выдача"
    ORDER = 20
    NAME = "OrderForm"
    ICON = "🪜"
    DESCRIPTION = ("Сначала спросить у покупателя нужные данные (ник, ID, ссылку, почту), проверить формат — "
                   "и только потом выдать товар или позвать вас.")
    DEFAULTS = {"rules": [], "expire_hours": 24,
                "text_ok": "Принято: $answer ✅", "text_done": "Готово! Спасибо за покупку 🙌"}
    FIELDS = [("rules", "Правила", "items"),
              ("text_ok", "Ответ, когда данные приняты", "area", "$answer — что прислал покупатель"),
              ("text_done", "Сообщение, когда вы отметили «выполнено»", "area"),
              ("expire_hours", "Сколько ждать ответа, ч", "number")]
    ITEM_FIELDS = {"rules": [
        ("keyword", "Слово в названии заказа", "text", "например: робуксы | robux"),
        ("question", "Что спросить", "area", "например: Пришлите ваш ник в Roblox"),
        ("regex", "Проверка ответа (regex, необязательно)", "text", r"например ^\S{3,20}$ — ник без пробелов"),
        ("bad", "Если формат неверный", "text", "например: Ник должен быть без пробелов"),
        ("then", "Потом", "select", [["manual", "позвать меня (выполню вручную)"], ["deliver", "выдать товар автоматически"]]),
        ("enabled", "Включено", "bool")]}

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("pending", {})   # "platform:buyer" → {...}

    def _rule(self, title):
        t = (title or "").lower()
        for i, r in enumerate(self.get("rules", [])):
            if r.get("enabled", True) is False:
                continue
            kws = [k.strip().lower() for k in str(r.get("keyword", "")).split("|") if k.strip()]
            if kws and any(k in t for k in kws):
                return i, r
        return None, None

    def on_order(self, o) -> bool:
        i, r = self._rule(o.title)
        if r is None:
            return False
        with self.lock:
            self.data["pending"][f"{o.platform}:{str(o.buyer).lower()}"] = dict(order_to_dict(o), rule=i, t=time.time())
            self.save()
        o.reply(fmt_template(r.get("question") or "Пришлите, пожалуйста, данные для выполнения заказа.",
                             username=o.buyer, order_id=o.id))
        return True

    def on_message(self, m) -> bool:
        key = f"{m.platform}:{str(m.author).lower()}"
        p = self.data["pending"].get(key)
        if not p or m.system:
            return False
        rules = self.get("rules", [])
        r = rules[p["rule"]] if p["rule"] < len(rules) else {}
        ans = (m.text or "").strip()
        rx = r.get("regex")
        if rx:
            try:
                ok = re.search(rx, ans) is not None
            except re.error:
                ok = True
            if not ok:
                self.reply(m.platform, m.chat_id, r.get("bad") or "Неверный формат, попробуйте ещё раз.", m.account, m.chat_name)
                return True
        with self.lock:
            self.data["pending"].pop(key, None)
            p["answer"] = ans
            self.data.setdefault("waiting", {})[p["id"]] = p
            self.save()
        self.reply(m.platform, m.chat_id, fmt_template(self.get("text_ok"), answer=ans, username=m.author), m.account, m.chat_name)
        o = order_from_dict(self.app, p)
        if r.get("then") == "deliver":
            self.data["waiting"].pop(p["id"], None)
            self.save()
            self.notify(f"🪜 <b>{esc(m.author)}</b> прислал данные по заказу <code>{esc(p['id'])}</code>: <code>{esc(ans)}</code> — выдаю")
            continue_order(self.app, o, skip=(self.KEY,))
        else:
            self.notify(f"🪜 <b>Нужно выполнить заказ</b> <code>{esc(p['id'])}</code>\n📦 {esc(p['title'])} ×{p['amount']}\n"
                        f"👤 {esc(m.author)}\n✍️ Данные: <code>{esc(ans)}</code>",
                        buttons=[[("✅ Выполнено", f"duo:mod:steps:act:done:{p['id']}", "delivery")]])
        return True

    def tick(self):
        lim = float(self.get("expire_hours", 24) or 24) * 3600
        with self.lock:
            for k, p in list(self.data["pending"].items()):
                if time.time() - p["t"] > lim:
                    self.data["pending"].pop(k)
                    self.notify(f"🪜 Покупатель {esc(p['buyer'])} не прислал данные по заказу <code>{esc(p['id'])}</code> "
                                f"за {self.get('expire_hours')} ч")
            self.save()

    def status(self) -> str:
        return f"ждут ответа: {len(self.data['pending'])} · ждут выполнения: {len(self.data.get('waiting', {}))}"

    def actions(self) -> list:
        return [(f"done:{oid}", f"✅ Выполнен #{oid}") for oid in list(self.data.get("waiting", {}))[:8]]

    def run_action(self, action, args) -> str:
        if action == "done" and args:
            p = self.data.get("waiting", {}).pop(args[0], None)
            self.save()
            if not p:
                return "Заказ уже отмечен"
            self.reply(p["platform"], p["chat_id"], fmt_template(self.get("text_done"), username=p["buyer"], order_id=p["id"]),
                       p.get("account"), p["buyer"])
            self.app.stats.set_order_flag(p["id"], "delivered", True, p["platform"])
            return "Отмечено, покупатель получил сообщение"
        return "Нет такого действия"


# ═════════════════════ наборы ═════════════════════
class BundlesModule(Module):
    KEY = "bundles"
    GROUP = "Выдача"
    ORDER = 40
    NAME = "Bundles"
    ICON = "🎁"
    DESCRIPTION = "Один заказ — несколько товаров из разных файлов (например, ключ + бонус)."
    DEFAULTS = {"rules": []}
    FIELDS = [("rules", "Наборы", "items")]
    ITEM_FIELDS = {"rules": [
        ("keyword", "Слово в названии заказа", "text"),
        ("files", "Файлы и количество", "lines", "по строке: keys.txt x1 — на каждую штуку в заказе"),
        ("text", "Сообщение", "area", "$product — все товары набора"),
        ("enabled", "Включено", "bool")]}

    def _parse(self, lines):
        out = []
        for ln in lines or []:
            m = re.match(r"\s*(.+?)\s*(?:[x×*]\s*(\d+))?\s*$", str(ln))
            if m and m.group(1):
                out.append((Products.safe_name(m.group(1)), int(m.group(2) or 1)))
        return out

    def on_order(self, o) -> bool:
        t = (o.title or "").lower()
        for r in self.get("rules", []):
            kws = [k.strip().lower() for k in str(r.get("keyword", "")).split("|") if k.strip()]
            if r.get("enabled", True) is False or not kws or not any(k in t for k in kws):
                continue
            need = [(f, n * o.amount) for f, n in self._parse(r.get("files"))]
            if not need:
                return False
            short = [f for f, n in need if Products.count(f) < n]
            if short:
                self.notify(f"🎁 <b>Не хватает товара для набора</b> по заказу <code>{esc(o.id)}</code>: "
                            + ", ".join(f"<code>{esc(f)}</code>" for f in short),
                            buttons=[[("📦 Выдать вручную", f"duo:manual:{o.account}:{o.id}", "delivery")]])
                return True
            got = []
            for f, n in need:
                got += Products.take(f, n) or []
            text = fmt_template(r.get("text") or "Ваш набор:\n$product", product="\n".join(got), username=o.buyer,
                                order_id=o.id)
            if o.reply(text):
                self.app.stats.inc("deliveries")
                self.app.stats.set_order_flag(o.id, "delivered", True, o.platform)
                self.notify(f"🎁 Выдан набор по заказу <code>{esc(o.id)}</code>")
            else:
                self.notify(f"❗️ Набор по заказу <code>{esc(o.id)}</code> не отправился — товар:\n<code>{esc(chr(10).join(got))[:3000]}</code>")
            return True
        return False

    def status(self) -> str:
        return f"наборов: {len(self.get('rules', []))}"


# ═════════════════════ бонус постоянным ═════════════════════
class LoyaltyModule(Module):
    KEY = "loyalty"
    GROUP = "Общение"
    ORDER = 5
    NAME = "Loyalty"
    ICON = "💎"
    DESCRIPTION = "После каждой N-й покупки отправляет покупателю промокод или подарок — люди возвращаются."
    DEFAULTS = {"every": 5, "text": "Спасибо, что вы с нами! Это ваш $orders_count-й заказ 🎉 Промокод на скидку: $code",
                "code": "THANKS10", "min_price": 0}
    FIELDS = [("every", "Каждая N-я покупка", "number"), ("code", "Промокод / подарок", "text"),
              ("min_price", "Только если заказ от, ₽", "number"),
              ("text", "Сообщение", "area", "$orders_count $code $username")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("sent", 0)

    def on_order(self, o) -> bool:
        cust = self.app.customers.get(o.platform, o.buyer) or {}
        n, every = int(cust.get("orders", 0)), int(self.get("every", 5) or 5)
        if n and every and n % every == 0 and float(o.price or 0) >= float(self.get("min_price") or 0):
            o.reply(fmt_template(self.get("text"), orders_count=n, code=self.get("code"), username=o.buyer))
            self.data["sent"] += 1
            self.save()
            self.notify(f"💎 Бонус отправлен {esc(o.buyer)} ({n}-я покупка)", kind="finance")
        return False

    def status(self) -> str:
        return f"бонусов отправлено: {self.data['sent']}"
