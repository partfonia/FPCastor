"""Лоты без товара: когда файл товаров опустел — сообщить / спросить / скрыть связанные лоты FunPay,
а после пополнения — предложить включить их обратно."""
from __future__ import annotations

import secrets
import threading
import time
from typing import TYPE_CHECKING

from .core import Products, esc, log

if TYPE_CHECKING:
    from .app import App


class StockManager:
    def __init__(self, app: "App"):
        self.app = app
        self.lock = threading.Lock()
        Products.on_change = self.on_products_change

    # ---------- связи правило → лоты ----------
    def rules_for_file(self, name) -> list:
        name = Products.safe_name(name)
        return [r for r in self.app.cfg.get("delivery", [])
                if r.get("mode", "file") == "file" and Products.safe_name(r.get("file", "")) == name]

    def lots_for_file(self, name) -> list:
        lots, seen = [], set()
        for r in self.rules_for_file(name):
            for lot in r.get("lots", []):
                k = f"{lot.get('account')}:{lot.get('id')}"
                if k not in seen:
                    seen.add(k)
                    lots.append(lot)
        return lots

    def hidden_for_file(self, name) -> list:
        name = Products.safe_name(name)
        return [dict(v, key=k) for k, v in self.app.state.data.get("hidden_lots", {}).items() if v.get("file") == name]

    # ---------- события ----------
    def on_products_change(self, name, old, new):
        try:
            if old > 0 and new == 0:
                self.out_of_stock(name)
            elif old == 0 and new > 0:
                self.restocked(name)
        except Exception as e:
            log.error("Лоты без товара (%s): %s", name, e)

    def _mode(self, key):
        return self.app.cfg.get(f"features.out_of_stock.{key}", "ask")

    def _pending(self, action, name, lots) -> str:
        nonce = secrets.token_hex(4)
        self.app.state.mark("pending", nonce, {"action": action, "file": name, "lots": lots,
                                                "exp": time.time() + 86400 * 3})
        return nonce

    def out_of_stock(self, name):
        name = Products.safe_name(name)
        if not self.rules_for_file(name):
            return
        flag = f"oos:{name}"
        if self.app.state.get("flags", flag):
            return
        self.app.state.mark("flags", flag)
        mode = self._mode("mode")
        if mode == "off":
            return
        lots = self.lots_for_file(name)
        fp_lots = [x for x in lots if str(x.get("account", "")).startswith("fp")]
        names = "\n".join(f"• {esc(x.get('title') or x.get('id'))} ({esc(self.app.account_name(x.get('account')))})"
                          for x in lots) or "— лоты не привязаны к правилу"
        head = f"📭 <b>Закончился товар</b> в <code>{esc(name)}</code>\n\nСвязанные лоты:\n{names}"
        if not fp_lots or mode == "notify":
            tip = "" if lots else "\n\nПривяжите лоты к правилу в мини-аппе (Выдача → правило → Лоты), и бот сможет скрывать их сам."
            self.app.notify("stock_out", head + tip, buttons=[[("📦 Пополнить", "duo:dlv", "products")]])
            return
        if mode == "auto":
            res = self.set_lots(fp_lots, False, name)
            self.app.notify("stock_out", head + "\n\n" + res)
            return
        nonce = self._pending("hide", name, fp_lots)
        self.app.notify("stock_out", head + "\n\nСкрыть лоты, пока товара нет?",
                        buttons=[[(f"🙈 Скрыть ({len(fp_lots)})", f"duo:stk:{nonce}:y", "delivery"),
                                  ("Оставить", f"duo:stk:{nonce}:n", "delivery")]])

    def restocked(self, name):
        name = Products.safe_name(name)
        self.app.state.pop("flags", f"oos:{name}")
        hidden = self.hidden_for_file(name)
        if not hidden:
            return
        mode = self._mode("restore")
        if mode == "off":
            return
        lots = [{"account": h["account"], "id": h["id"], "title": h.get("title", "")} for h in hidden]
        head = f"📬 Товар в <code>{esc(name)}</code> снова есть ({Products.count(name)} шт.)."
        if mode == "auto":
            self.app.notify("stock_out", head + "\n\n" + self.set_lots(lots, True, name))
            return
        nonce = self._pending("show", name, lots)
        self.app.notify("stock_out", head + f"\nВключить обратно скрытые лоты ({len(lots)})?",
                        buttons=[[("👁 Включить", f"duo:stk:{nonce}:y", "delivery"),
                                  ("Не сейчас", f"duo:stk:{nonce}:n", "delivery")]])

    def resolve(self, nonce, yes: bool) -> str:
        with self.lock:
            p = self.app.state.pop("pending", nonce)
        if not p or p.get("exp", 0) < time.time():
            return "Действие уже выполнено или устарело"
        if not yes:
            return "Хорошо, лоты не трогаю"
        return self.set_lots(p["lots"], p["action"] == "show", p["file"])

    # ---------- включение / выключение ----------
    def set_lots(self, lots, active: bool, file="") -> str:
        lines = []
        for lot in lots:
            acc, lid = lot.get("account"), str(lot.get("id"))
            svc = self.app.fp.get(acc)
            title = lot.get("title") or lid
            if not svc or not svc.ok:
                lines.append(f"⚠️ {esc(title)}: аккаунт {esc(acc)} не подключён")
                continue
            try:
                svc.client.set_lot_active(lid, active)
                key = f"{acc}:{lid}"
                if active:
                    self.app.state.pop("hidden_lots", key)
                else:
                    self.app.state.mark("hidden_lots", key, {"account": acc, "id": lid, "title": title,
                                                             "file": Products.safe_name(file) if file else "",
                                                             "t": time.time()})
                lines.append(f"{'👁 включён' if active else '🙈 скрыт'}: {esc(title)}")
                log.info("Лот %s (%s) %s", lid, acc, "включён" if active else "скрыт")
            except Exception as e:
                lines.append(f"❌ {esc(title)}: {esc(e)}")
            time.sleep(0.8)
        return "\n".join(lines) or "Нет лотов"
