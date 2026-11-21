"""Сервисы площадок: циклы опроса FunPay и слушатель событий Playerok."""
from __future__ import annotations

import re
import threading
import time
import traceback
from datetime import datetime
from typing import TYPE_CHECKING

from .branding import is_bot_message
from .compat import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
from .core import UMessage, UOrder, _g, esc, fmt_template, log
from .funpay import FPMessage, FunPayClient, MessageTypes, OrderStatuses
from .playerok import PlayerokBridge

if TYPE_CHECKING:
    from .app import App


class FunPayService:
    def __init__(self, app: "App", acc: dict):
        self.app = app
        self.acc = acc
        self.acc_id = acc["id"]
        cfg = app.cfg
        self.client = FunPayClient(acc.get("golden_key", ""), acc.get("user_agent") or cfg.get("funpay.user_agent"),
                                   acc.get("proxy", ""))
        self.active = True
        self.ok = False
        self.error = ""
        self.last_msg_id: dict = {}
        self.order_status: dict = {}
        self.counters = None
        self.bm_tag = None
        self.cnt_tag = None
        self.next_raise: dict = {}     # game_id -> timestamp
        self.raise_info: dict = {}     # game_id -> текст статуса
        self.lot_cache = None
        self.last_orders_fetch = 0

    def apply_proxy(self, proxy: str):
        self.client.set_proxy(proxy)
        log.info("🔵 FunPay: прокси → %s", proxy or "напрямую")

    @property
    def label(self):
        return self.app.account_name(self.acc_id)

    def stop(self):
        self.active = False
        self.ok = False

    # ---------- запуск ----------
    def start(self):
        threading.Thread(target=self._loop, name=f"funpay-{self.acc_id}", daemon=True).start()
        threading.Thread(target=self._raise_loop, name=f"fp-raise-{self.acc_id}", daemon=True).start()

    def _login(self):
        delay = 5
        while self.app.running and self.active:
            try:
                self.client.login()
                self.ok, self.error = True, ""
                log.info("🔵 FunPay [%s]: вошёл как %s (id %s), баланс %s %s", self.acc_id, self.client.username, self.client.id,
                         self.client.balance, self.client.currency)
                return True
            except Exception as e:
                self.ok, self.error = False, str(e)
                log.error("🔵 FunPay: ошибка входа: %s (повтор через %sс)", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)
        return False

    def _loop(self):
        if not self._login():
            return
        self.app.plugins.call("BIND_TO_POST_START", self.app.cardinal)
        first = True
        errors = 0
        while self.app.running and self.active:
            try:
                res = self.client.poll(self.cnt_tag, self.bm_tag)
                changed_orders = False
                for o in res.get("objects", []):
                    if o.get("type") == "chat_bookmarks":
                        self.bm_tag = o.get("tag")
                        chats = self.client.parse_bookmarks((o.get("data") or {}).get("html", ""))
                        self._process_chats(chats, first)
                    elif o.get("type") == "orders_counters":
                        self.cnt_tag = o.get("tag")
                        data = o.get("data")
                        if data != self.counters:
                            changed_orders = self.counters is not None or first
                            self.counters = data
                if changed_orders or first or time.time() - self.last_orders_fetch > 90:
                    self._process_orders(first)
                first = False
                errors = 0
                self.ok = True
            except Exception as e:
                errors += 1
                self.error = str(e)
                log.warning("🔵 FunPay: ошибка цикла: %s", e)
                if errors in (3, 10):
                    self.app.notify_error(f"FunPay: {e}")
                if errors >= 3:
                    self.ok = False
                    try:
                        self.client.login()
                    except Exception:
                        pass
                time.sleep(min(60, 5 * errors))
            time.sleep(max(2, float(self.app.cfg.get("funpay.poll_interval", 6))))

    # ---------- чаты ----------
    def _process_chats(self, chats: list, first: bool):
        for ch in chats:
            cid = ch["id"]
            last = self.last_msg_id.get(cid)
            if first:
                self.last_msg_id[cid] = ch["node_msg"]
                continue
            if last is None:
                last = 0
            if ch["node_msg"] <= last:
                continue
            try:
                msgs = self.client.get_chat_history(cid, ch["name"])
            except Exception as e:
                log.warning("FunPay: история чата %s: %s", cid, e)
                continue
            new = [m for m in msgs if m.id > last] if last else msgs[-1:]
            self.last_msg_id[cid] = max([ch["node_msg"]] + [m.id for m in msgs])
            for m in new:
                self._handle_fp_message(m)

    def _handle_fp_message(self, m: FPMessage):
        by_me = str(m.author_id) == str(self.client.id)
        system = str(m.author_id) == "0"
        m.by_bot = by_me and is_bot_message(self.app.cfg, m.text or "")
        um = UMessage("funpay", m.chat_id, m.text or (m.image_link or ""), m.author, m.author_id, m.chat_name,
                      m.id, by_me, system, raw=m, account=self.acc_id)
        self.app.plugins.call("BIND_TO_NEW_MESSAGE", self.app.cardinal, NewMessageEvent(self.client.tag, m))
        if system:
            self._handle_system(m)
        self.app.on_message(um)

    def _handle_system(self, m: FPMessage):
        oid_m = re.search(r"#([A-Z0-9]{6,10})", m.text or "")
        if not oid_m:
            return
        oid = oid_m.group(1)
        cfg = self.app.cfg
        if m.type in (MessageTypes.ORDER_CONFIRMED, MessageTypes.ORDER_CONFIRMED_BY_ADMIN):
            if self.app.state.get("confirmed", "fp:" + oid):
                return
            self.app.state.mark("confirmed", "fp:" + oid)
            self.app.state.pop("open_orders", "funpay:" + oid)
            buyer = m.chat_name
            self.app.notify("fp_confirm", f"✅ <b>FunPay{self.app.acc_tag(self.acc_id)}</b>: заказ <code>#{oid}</code> подтверждён ({esc(buyer)})",
                            buttons=[[("🔗 Заказ", f"https://funpay.com/orders/{oid}/")]])
            if cfg.get("funpay.confirm_reply.enabled"):
                self.app.send("funpay", m.chat_id, fmt_template(cfg.get("funpay.confirm_reply.text"),
                                                                   username=buyer, order_id=oid), account=self.acc_id)
        elif m.type in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
            stars, rtext = None, ""
            try:
                order = self.client.get_order(oid)
                if order.review:
                    stars, rtext = order.review.stars, order.review.text
            except Exception as e:
                log.warning("FunPay: не получил отзыв %s: %s", oid, e)
            self.app.notify("fp_review", f"⭐ <b>FunPay{self.app.acc_tag(self.acc_id)}</b>: отзыв к <code>#{oid}</code> от {esc(m.chat_name)}: "
                                         f"{'⭐' * (stars or 0)}\n{esc(rtext)}",
                            buttons=[[("🔗 Заказ", f"https://funpay.com/orders/{oid}/")]])
            if cfg.get("funpay.review_reply.enabled") and stars:
                key = f"fp:{oid}"
                if self.app.state.get("reviewed", key) != stars:
                    t = (cfg.get("funpay.review_reply.texts") or {}).get(str(stars))
                    if t:
                        try:
                            self.client.send_review(oid, fmt_template(t, username=m.chat_name, order_id=oid))
                            self.app.state.mark("reviewed", key, stars)
                        except Exception as e:
                            log.warning("FunPay: ответ на отзыв %s: %s", oid, e)

    # ---------- заказы ----------
    def _process_orders(self, first: bool):
        self.last_orders_fetch = time.time()
        orders = self.client.get_sales()
        for o in reversed(orders):
            prev = self.order_status.get(o.id)
            self.order_status[o.id] = o.status
            if first:
                continue
            if prev is None and o.status == OrderStatuses.PAID:
                o.chat_id = self.client.chat_for(o.buyer_username, o.buyer_id)
                self.app.plugins.call("BIND_TO_NEW_ORDER", self.app.cardinal, NewOrderEvent(self.client.tag, o))
                uo = UOrder("funpay", o.id, o.description, o.price, o.buyer_username, o.buyer_id, o.chat_id,
                            o.amount, "paid", raw=o, currency=o.currency, account=self.acc_id)
                self.app.on_order(uo)
            elif prev is not None and prev != o.status:
                if o.status != OrderStatuses.PAID:
                    self.app.state.pop("open_orders", f"funpay:{o.id}")
                if o.status == OrderStatuses.REFUNDED:
                    self.app.stats.set_order_flag(o.id, "refunded", True, "funpay")
                self.app.plugins.call("BIND_TO_ORDER_STATUS_CHANGED", self.app.cardinal,
                                      OrderStatusChangedEvent(self.client.tag, o))

    # ---------- поднятие ----------
    def _raise_loop(self):
        while self.app.running and self.active:
            time.sleep(15)
            if not self.ok or not self.app.cfg.get("funpay.auto_raise.enabled") or not self.acc.get("auto_raise", True):
                continue
            try:
                self.raise_all()
            except Exception as e:
                log.warning("FunPay: автоподнятие: %s", e)
            time.sleep(max(1, int(self.app.cfg.get("funpay.auto_raise.check_every_min", 10))) * 60)

    def _groups(self, refresh=False) -> dict:
        if self.lot_cache is None or refresh:
            groups: dict = {}
            for n in self.client.get_my_lot_nodes():
                try:
                    gid = self.client.get_game_id(n["node"])
                except Exception:
                    gid = None
                if gid:
                    groups.setdefault(gid, {"nodes": [], "names": []})
                    groups[gid]["nodes"].append(n["node"])
                    groups[gid]["names"].append(n["name"])
                time.sleep(0.5)
            self.lot_cache = groups
        return self.lot_cache

    def raise_all(self, force=False) -> list:
        report = []
        groups = self._groups(refresh=force)
        self.app.plugins.call("BIND_TO_PRE_LOTS_RAISE", self.app.cardinal, groups)
        for gid, g in groups.items():
            if not force and time.time() < self.next_raise.get(gid, 0):
                continue
            res = self.client.raise_lots(gid, g["nodes"])
            msg = re.sub(r"<[^>]+>", "", str(res.get("msg") or res.get("modal") or ""))
            name = ", ".join(g["names"][:3])
            if res.get("error") or re.search(r"подожд|wait|зачекай", msg, re.I):
                wait = self._parse_wait(msg)
                self.next_raise[gid] = time.time() + wait
                self.raise_info[gid] = f"⏳ {name}: {msg or 'ожидание'}"
            else:
                self.next_raise[gid] = time.time() + 3600
                self.raise_info[gid] = f"⬆️ {name}: подняты {datetime.now():%H:%M}"
                self.app.stats.inc("raises")
                log.info("🔵 FunPay: подняты лоты %s", name)
                self.app.notify("raise", f"⬆️ FunPay{self.app.acc_tag(self.acc_id)}: подняты лоты <b>{esc(name)}</b>")
            report.append(self.raise_info[gid])
            time.sleep(1)
        self.app.plugins.call("BIND_TO_POST_LOTS_RAISE", self.app.cardinal, groups)
        return report

    @staticmethod
    def _parse_wait(msg: str) -> int:
        m = re.search(r"(\d+)\s*(сек|sec|мин|min|час|hour|год)", msg or "", re.I)
        if not m:
            return 600
        n, u = int(m.group(1)), m.group(2).lower()
        if u.startswith(("сек", "sec")):
            return n + 5
        if u.startswith(("мин", "min")):
            return n * 60 + 10
        return n * 3600 + 30


class PlayerokService:
    def __init__(self, app: "App", acc: dict):
        self.app = app
        self.acc = acc
        self.acc_id = acc["id"]
        self.bridge = PlayerokBridge(app.cfg, acc)
        self.active = True
        self.ok = False
        self.error = ""

    @property
    def label(self):
        return self.app.account_name(self.acc_id)

    def stop(self):
        self.active = False
        self.ok = False

    def start(self):
        threading.Thread(target=self._loop, name=f"playerok-{self.acc_id}", daemon=True).start()

    def _loop(self):
        delay = 5
        while self.app.running and self.active:
            try:
                if not self.bridge.ok:
                    self.bridge.connect()
                    log.info("🟢 Playerok [%s]: вошёл как %s (id %s)", self.acc_id, self.bridge.username, self.bridge.id)
                    self.app.plugins.call("BIND_TO_PL_BOT_INIT", self.app.pl_compat)
                self.ok, self.error = True, ""
                delay = 5
                for ev in self.bridge.listen():
                    if not self.app.running or not self.active:
                        return
                    try:
                        self._dispatch(ev)
                    except Exception as e:
                        log.error("Playerok: обработка события: %s", e)
                        log.debug(traceback.format_exc())
            except Exception as e:
                self.ok, self.error = False, str(e)
                log.error("🟢 Playerok: %s (повтор через %sс)", e, delay)
                if delay >= 80:
                    self.app.notify_error(f"Playerok: {e}")
                    self.bridge.ok = False
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def _dispatch(self, ev):
        et = getattr(getattr(ev, "type", None), "name", type(ev).__name__)
        self.app.plugins.call_pl("BIND_TO_PL_" + et, ev)
        cfg = self.app.cfg
        chat_id = _g(ev, "chat", "id")
        if et == "NEW_MESSAGE":
            msg = ev.message
            uid = _g(msg, "user", "id")
            uname = _g(msg, "user", "username", default="")
            text = _g(msg, "text", default="") or ""
            if _g(msg, "file", "url"):
                text = (text + " " + _g(msg, "file", "url")).strip()
            by_me = uid is not None and str(uid) == str(self.bridge.id)
            system = uid is None
            um = UMessage("playerok", chat_id, text, uname, uid, uname, _g(msg, "id"), by_me, system, raw=ev,
                          account=self.acc_id)
            self.app.on_message(um)
        elif et == "NEW_DEAL":
            deal = ev.deal
            price = _g(deal, "item", "price", default=0) or _g(deal, "transaction", "value", default=0)
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = 0.0
            uo = UOrder("playerok", _g(deal, "id"), _g(deal, "item", "name", default="") or "",
                        price, _g(deal, "user", "username", default=""), _g(deal, "user", "id"),
                        chat_id or _g(deal, "chat", "id"), _g(deal, "amount", default=1) or 1, raw=ev,
                        account=self.acc_id)
            self.app.on_order(uo)
        elif et == "DEAL_CONFIRMED":
            deal = ev.deal
            did = _g(deal, "id")
            if self.app.state.get("confirmed", f"pl:{did}"):
                return
            self.app.state.mark("confirmed", f"pl:{did}")
            self.app.state.pop("open_orders", f"playerok:{did}")
            uname = _g(deal, "user", "username", default="")
            self.app.notify("pl_confirm", f"✅ <b>Playerok{self.app.acc_tag(self.acc_id)}</b>: сделка подтверждена — {esc(_g(deal, 'item', 'name', default=''))} "
                                          f"({esc(uname)})")
            if cfg.get("playerok.confirm_reply.enabled") and chat_id:
                self.app.send("playerok", chat_id, fmt_template(cfg.get("playerok.confirm_reply.text"),
                                                                   username=uname, order_id=did), account=self.acc_id)
        elif et == "NEW_REVIEW":
            deal = _g(ev, "deal")
            rating = _g(deal, "review", "rating", default="?")
            text = _g(deal, "review", "text", default="")
            self.app.notify("pl_review", f"⭐ <b>Playerok</b>: новый отзыв {rating}/5 от "
                                         f"{esc(_g(deal, 'user', 'username', default=''))}\n{esc(text)}")
        elif et in ("DEAL_HAS_PROBLEM", "DEAL_ROLLED_BACK"):
            deal = _g(ev, "deal")
            if et == "DEAL_ROLLED_BACK":
                self.app.state.pop("open_orders", f"playerok:{_g(deal, 'id')}")
                self.app.stats.set_order_flag(_g(deal, "id"), "refunded", True, "playerok")
            self.app.notify("pl_deal", f"⚠️ <b>Playerok</b>: {'проблема по сделке' if et == 'DEAL_HAS_PROBLEM' else 'возврат сделки'} — "
                                       f"{esc(_g(deal, 'item', 'name', default=''))} ({esc(_g(deal, 'user', 'username', default=''))})")
