"""Ядро: аккаунты площадок, обработка сообщений и заказов, автовыдача, прибыль."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from . import APP_NAME, VERSION
from .compat import CardinalCompat, PlayerokCompatBot
from .core import (BASE_DIR, PLUGINS_DIR, Config, Customers, Products, State, Stats, UMessage, UOrder,
                   esc, fmt_template, human_uptime, in_work_hours, log, proxy_label)
from .features import AntiFlood, LowStock, Scheduler, Webhooks
from .branding import apply_watermark, bot_description, bot_short_description, description_hash  # noqa: F401
from .finance import WithdrawManager
from .plugins import PluginManager
from .proxy import ProxyManager
from .roles import NOTIFY_PERM, Team
from .services import FunPayService, PlayerokService
from .stock import StockManager
from .suppliers import SupplierError, Suppliers

try:
    import telebot  # noqa: F401
    HAS_TELEBOT = True
except ImportError:
    HAS_TELEBOT = False

CRED_KEYS = {"funpay": ("golden_key", "proxy", "user_agent"), "playerok": ("token", "cookies", "proxy", "user_agent")}


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State()
        self.stats = Stats(cfg)
        self.customers = Customers()
        self.team = Team(cfg, self.state)
        self.running = True
        self.events: deque = deque(maxlen=400)
        self.fp: dict[str, FunPayService] = {}
        self.pl: dict[str, PlayerokService] = {}
        self.tg = None
        self.web = None
        self.proxy = ProxyManager(cfg)
        self.cardinal = CardinalCompat(self)
        self.pl_compat = PlayerokCompatBot(self)
        self.plugins = PluginManager(self)
        self.flood = AntiFlood(self)
        self.webhooks = Webhooks(self)
        self.lowstock = LowStock(self)
        self.scheduler = Scheduler(self)
        from .modules import ModuleManager
        self.modules = ModuleManager(self)
        self.stock = StockManager(self)
        self.suppliers = Suppliers(self)
        self.withdraw = WithdrawManager(self)
        self.public_url = cfg.get("web.public_url", "")
        self._send_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        cfg.listeners.append(self._on_config_change)

    # ═════════ запуск / остановка ═════════
    def start(self):
        log.info("🚀 %s v%s запускается…", APP_NAME, VERSION)
        self.onedrive = "onedrive" in str(BASE_DIR).lower()
        if self.onedrive:
            log.warning("☁️ Бот лежит в папке OneDrive (%s). OneDrive блокирует файлы, которые бот постоянно "
                        "сохраняет, — возможны ошибки и потеря настроек. Перенесите папку, например в C:\\FPCastor", BASE_DIR)
        os.chdir(BASE_DIR)
        for p in (str(BASE_DIR), str(PLUGINS_DIR)):
            if p not in sys.path:
                sys.path.insert(0, p)
        self.plugins.load_all()
        self.sync_accounts(start=False)
        if self.cfg.get("telegram.enabled") and self.cfg.get("telegram.token") and HAS_TELEBOT:
            from .telegram_ui import TelegramUI
            self.tg = TelegramUI(self)
        self.plugins.call("BIND_TO_PRE_INIT", self.cardinal)
        self.plugins.call("BIND_TO_PU_INIT")
        if self.cfg.get("web.enabled"):
            from .web import WebServer
            self.web = WebServer(self)
            self.web.start()
        if self.tg:
            self.tg.start()
        self.plugins.call("BIND_TO_POST_INIT", self.cardinal)
        if self.tg:
            self.tg.register_fallbacks()
        self.cardinal.running = True
        self.plugins.call("BIND_TO_PRE_START", self.cardinal)
        for svc in list(self.fp.values()) + list(self.pl.values()):
            svc.start()
        self.scheduler.start()
        if not self.fp and not self.pl:
            log.warning("⚠️ Аккаунты не добавлены. Откройте мини-апп / веб-панель или запустите: python main.py --setup")
        if not self.team.users() and self.cfg.get("telegram.token"):
            log.warning("👤 В команде нет владельца. Напишите боту: /login %s", self.cfg.get("telegram.password"))
        self.notify("start", f"🚀 <b>{APP_NAME}</b> v{VERSION} запущен\n"
                             f"🔵 FunPay: {len(self.fp)} акк. · 🟢 Playerok: {len(self.pl)} акк.\n"
                             f"🌐 Прокси TG: {self.proxy.label()}"
                             + (f"\n📱 Панель: {self.public_url}" if self.public_url and self.cfg.get("web.tunnel") == "none" else
                                "\n📱 Приложение подключается — пришлю сообщение, когда заработает"
                                if self.cfg.get("web.enabled") and self.cfg.get("web.tunnel") != "none" else "")
                             + ("\n\n☁️ <b>Бот лежит в папке OneDrive</b> — он может мешать сохранять настройки. "
                                "Перенесите папку FPCastor, например в <code>C:\\FPCastor</code>." if self.onedrive else ""))

    def stop(self):
        self.running = False
        self.plugins.call("BIND_TO_PRE_STOP", self.cardinal)
        for svc in list(self.fp.values()) + list(self.pl.values()):
            svc.stop()
        if self.tg:
            self.tg.shutdown()
        if self.web:
            self.web.stop()
        self.plugins.call("BIND_TO_POST_STOP", self.cardinal)

    def restart(self):
        log.info("🔄 Перезапуск…")
        self.stop()
        time.sleep(1)
        args = [a for a in sys.argv[1:] if a not in ("--setup",)]
        if "--no-dialog" not in args:
            args.append("--no-dialog")
        os.execv(sys.executable, [sys.executable, str(BASE_DIR / "main.py")] + args)

    # ═════════ аккаунты ═════════
    def _wanted(self, platform) -> dict:
        if not self.cfg.get(f"{platform}.enabled", True):
            return {}
        keys = ("golden_key",) if platform == "funpay" else ("token", "cookies")
        return {a["id"]: a for a in self.cfg.accounts(platform)
                if a.get("enabled", True) and a.get("id") and any(a.get(k) for k in keys)}

    def sync_accounts(self, start=True):
        """Запускает новые аккаунты, останавливает удалённые, переподключает изменённые — без перезапуска бота."""
        with self._sync_lock:
            for platform, pool, cls in (("funpay", self.fp, FunPayService), ("playerok", self.pl, PlayerokService)):
                wanted = self._wanted(platform)
                for acc_id in list(pool):
                    svc = pool[acc_id]
                    w = wanted.get(acc_id)
                    if not w or any(w.get(k) != svc.acc.get(k) for k in CRED_KEYS[platform]):
                        svc.stop()
                        pool.pop(acc_id)
                        log.info("⏹ Аккаунт %s остановлен", acc_id)
                    else:
                        svc.acc = w  # обновить флаги (автоподнятие и т.п.)
                for acc_id, acc in wanted.items():
                    if acc_id not in pool:
                        svc = cls(self, acc)
                        pool[acc_id] = svc
                        if start and self.running:
                            svc.start()
                            log.info("▶️ Аккаунт %s запущен", acc_id)

    def account_name(self, acc_id) -> str:
        a = self.cfg.account(acc_id) if acc_id else None
        if not a:
            return acc_id or "—"
        svc = self.fp.get(acc_id) or self.pl.get(acc_id)
        uname = (svc.client.username if isinstance(svc, FunPayService) else svc.bridge.username) if svc else None
        return a.get("name") or uname or acc_id

    def acc_tag(self, acc_id) -> str:
        """« · Имя» — только если у площадки больше одного аккаунта."""
        platform = "funpay" if str(acc_id).startswith("fp") else "playerok"
        return f" · {self.account_name(acc_id)}" if len(self.cfg.accounts(platform)) > 1 else ""

    def service(self, platform, account=None):
        pool = self.fp if platform == "funpay" else self.pl
        if account and account in pool:
            return pool[account]
        return next((s for s in pool.values() if s.ok), None) or next(iter(pool.values()), None)

    # ═════════ горячее применение настроек ═════════
    def _on_config_change(self, old: dict, new: dict):
        logging.getLogger().setLevel(new["general"].get("log_level", "INFO"))
        if new["web"].get("public_url") and new["web"]["public_url"] != old["web"].get("public_url"):
            self.public_url = new["web"]["public_url"]
            if self.tg:
                self.tg.setup_menu_button()
        if (new.get("accounts") != old.get("accounts") or new["funpay"].get("enabled") != old["funpay"].get("enabled")
                or new["playerok"].get("enabled") != old["playerok"].get("enabled")):
            threading.Thread(target=self.sync_accounts, daemon=True).start()
        t_old, t_new = old["telegram"], new["telegram"]
        if (t_new.get("token") != t_old.get("token") or t_new.get("proxy") != t_old.get("proxy")
                or t_new.get("enabled") != t_old.get("enabled")):
            threading.Thread(target=self.reload_telegram, daemon=True).start()
        elif new.get("project") != old.get("project") and self.tg:
            threading.Thread(target=self.tg.update_description, daemon=True).start()

    def reload_telegram(self) -> tuple:
        if not HAS_TELEBOT:
            return False, "pyTelegramBotAPI не установлен"
        if not self.cfg.get("telegram.enabled") or not self.cfg.get("telegram.token"):
            if self.tg:
                self.tg.shutdown()
                self.tg = None
            return True, "Telegram выключен"
        if self.tg:
            return self.tg.reload()
        from .telegram_ui import TelegramUI
        self.tg = TelegramUI(self)
        self.tg.start()
        self.tg.register_fallbacks()
        return bool(self.tg.ok), ("@" + str(self.tg.username)) if self.tg.ok else self.tg.error

    # ═════════ отправка / уведомления ═════════
    def send(self, platform, chat_id, text, chat_name=None, account=None, manual=False) -> bool:
        """manual=True — ответ человека из Telegram / мини-аппа (подпись по настройке watermark_manual)."""
        if not text or chat_id is None or str(chat_id) == "None":
            return False
        if getattr(self, "modules", None):
            text = self.modules.on_send(platform, chat_id, text, manual)  # перевод и т.п.
        text = apply_watermark(self.cfg, text, manual)
        svc = self.service(platform, account)
        if not svc or not svc.ok and platform == "playerok":
            return False
        with self._send_lock:
            try:
                if platform == "funpay":
                    svc.client.send_message(chat_id, text, chat_name)
                else:
                    svc.bridge.send_message(chat_id, text)
                time.sleep(float(self.cfg.get("general.send_delay_sec", 0.5)))
                return True
            except Exception as e:
                log.error("Отправка (%s/%s → %s): %s", platform, getattr(svc, "acc_id", "?"), chat_id, e)
                return False

    def auto_send(self, platform, chat_id, text, chat_name=None, account=None) -> bool:
        if not self.flood.allow(f"{platform}:{account}", chat_id):
            log.info("🛡 Антифлуд: пропущен автоответ в %s:%s", platform, chat_id)
            return False
        return self.send(platform, chat_id, text, chat_name, account)

    def notify(self, kind: str, text: str, buttons=None, limited: str = None):
        """kind → право (roles.NOTIFY_PERM). limited — версия текста без денег для тех, у кого нет finance.
        Кнопка может быть (текст, data, право) — её увидят только те, у кого есть это право."""
        if self.tg and self.cfg.get(f"telegram.notify.{kind}", True):
            self.tg.notify(text, buttons, perm=NOTIFY_PERM.get(kind, "system"), limited=limited)
        plain = re.sub(r"<[^>]+>", "", limited or text or "")
        if kind.endswith(("_order", "_deal")):
            self.webhooks.emit("order", plain, {})
        elif kind.endswith("_message"):
            self.webhooks.emit("message", plain, {})
        elif kind == "delivery":
            self.webhooks.emit("delivery", plain, {})

    def notify_error(self, text):
        self.notify("errors", f"❗️ {esc(text)}")

    def push_event(self, kind, data: dict):
        self.events.append(dict(data, kind=kind))

    # ═════════ сообщения ═════════
    def _blacklisted(self, name) -> bool:
        return str(name).lower() in [str(u).lower() for u in self.cfg.get("blacklist.users", [])]

    def on_message(self, m: UMessage):
        m._app = self
        self.plugins.call("BIND_TO_ANY_MESSAGE", self.cardinal, m)
        self.push_event("message", m.to_dict())
        if m.by_me:
            return
        p, acc = m.platform, m.account
        pk = "fp" if p == "funpay" else "pl"
        cfg = self.cfg
        if not m.system:
            self.stats.inc("messages", p)
        bl = self._blacklisted(m.author)
        cust = self.customers.get(p, m.author)
        url = (f"https://funpay.com/chat/?node={m.chat_id}" if p == "funpay"
               else f"https://playerok.com/chats/{m.chat_id}")
        icon = ("🔵 FunPay" if p == "funpay" else "🟢 Playerok") + self.acc_tag(acc)
        badge = (" 🚫" if bl else "") + (f" ⭐{cust['orders']}" if cust and cust.get("orders") else "")
        note = f"\n📝 <i>{esc(cust['note'])}</i>" if cust and cust.get("note") else ""
        btns = [[("↩️ Ответить", f"duo:reply:{acc}:{m.chat_id}"), ("📝 Шаблоны", f"duo:tpl:{acc}:{m.chat_id}")],
                [("🔗 Открыть чат", url)]] if not m.system else [[("🔗 Открыть чат", url)]]
        self.notify(f"{pk}_message", f"💬 <b>{icon}</b> · <b>{esc(m.author or m.chat_name)}</b>{badge}\n"
                                     f"{esc(m.text)[:3500]}{note}", buttons=btns)
        if m.system or (bl and cfg.get("blacklist.block_reply")):
            return
        g = cfg.get(f"{p}.greeting", {})
        if g.get("enabled"):
            key = f"{acc}:{m.chat_id}"
            last = self.state.get("greeted", key)
            if not last or time.time() - last > float(g.get("cooldown_days", 7)) * 86400:
                self.state.mark("greeted", key)
                self.auto_send(p, m.chat_id, fmt_template(g.get("text"), username=m.author, platform=p,
                                                          chat_id=m.chat_id), m.chat_name, acc)
        vac = cfg.get("features.vacation", {})
        away_text = None
        if vac.get("enabled"):
            away_text = fmt_template(vac.get("text"), username=m.author, vacation_until=vac.get("until") or "—")
        elif not in_work_hours(cfg):
            away_text = fmt_template(cfg.get("general.work_hours.away_text"), username=m.author)
        if away_text:
            key = f"{acc}:{m.chat_id}:{datetime.now():%Y%m%d}"
            if not self.state.get("away", key):
                self.state.mark("away", key)
                self.auto_send(p, m.chat_id, away_text, m.chat_name, acc)
        if self.modules.on_message(m):  # !guard, !код и т.п. — модули отвечают сами
            return
        if cfg.get(f"{p}.auto_reply", True):
            txt = m.text.strip().lower()
            for c in cfg.get("commands", []):
                if c.get("enabled", True) is False or p not in c.get("platforms", ["funpay", "playerok"]):
                    continue
                trig = [t.strip().lower() for t in str(c.get("trigger", "")).split("|") if t.strip()]
                if any((txt == t) if c.get("match", "exact") == "exact" else (t in txt) for t in trig):
                    self.auto_send(p, m.chat_id, fmt_template(c.get("text"), username=m.author, platform=p,
                                                              chat_id=m.chat_id), m.chat_name, acc)
                    if c.get("notify"):
                        self.notify(f"{pk}_message", f"🔔 <b>{esc(m.author)}</b> вызвал <code>{esc(m.text)}</code> "
                                                     f"({icon})", buttons=[[("↩️ Ответить", f"duo:reply:{acc}:{m.chat_id}")]])
                    break

    # ═════════ заказы ═════════
    def on_order(self, o: UOrder):
        o._app = self
        self.stats.add_order(o)
        cust = self.customers.add_order(o)
        self.push_event("order", o.to_dict())
        self.state.mark("open_orders", f"{o.platform}:{o.id}",
                        {"platform": o.platform, "account": o.account, "id": str(o.id), "chat_id": o.chat_id,
                         "buyer": o.buyer, "title": o.title, "t": time.time()})
        self.plugins.call("BIND_TO_ANY_ORDER", self.cardinal, o)
        p, acc = o.platform, o.account
        bl = self._blacklisted(o.buyer)
        icon = ("🔵 FunPay" if p == "funpay" else "🟢 Playerok") + self.acc_tag(acc)
        if p == "funpay":
            btns = [[("🔗 Заказ", f"https://funpay.com/orders/{o.id}/"), ("💸 Возврат", f"duo:refund:{acc}:{o.id}", "refund")],
                    [("↩️ Написать", f"duo:reply:{acc}:{o.chat_id}", "chat"), ("📦 Выдать", f"duo:manual:{acc}:{o.id}", "delivery")]]
        else:
            btns = [[("↩️ Написать", f"duo:reply:{acc}:{o.chat_id}", "chat"), ("📤 Отправлен", f"duo:plsent:{acc}:{o.id}", "delivery")],
                    [("🔗 Сделка", f"https://playerok.com/deal/{o.id}")]]
        rep = f"\n⭐ Постоянный: {cust['orders']}-й заказ" if cust["orders"] > 1 else ""
        head = f"💰 <b>{icon}: новый заказ</b>{' 🚫ЧС' if bl else ''}\n🆔 <code>{esc(o.id)}</code>\n📦 {esc(o.title)}\n"
        tail = f"👤 {esc(o.buyer)}{rep}"
        self.notify("fp_order" if p == "funpay" else "pl_deal",
                    head + f"💵 {o.price} {esc(o.currency)} · ×{o.amount}\n" + tail + (f" · всего {cust['spent']}" if rep else ""),
                    buttons=btns, limited=head + f"×{o.amount}\n" + tail)
        cfg = self.cfg
        rb = cfg.get("features.returning_buyer", {})
        if rb.get("enabled") and cust["orders"] > 1:
            o.reply(fmt_template(rb.get("text"), username=o.buyer, orders_count=cust["orders"], order_id=o.id))
        if cfg.get(f"{p}.order_text.enabled"):
            o.reply(fmt_template(cfg.get(f"{p}.order_text.text"), username=o.buyer, order_id=o.id,
                                 order_title=o.title, price=o.price))
        if bl and cfg.get("blacklist.block_delivery"):
            self.notify("delivery", f"🚫 Покупатель {esc(o.buyer)} в ЧС — автовыдача пропущена")
            return
        if cfg.get("features.vacation.enabled") and cfg.get("features.vacation.disable_delivery"):
            return
        acc_cfg = cfg.account(acc) or {}
        if self.modules.on_order(o):  # аренда и другие модули выдают сами
            return
        if cfg.get(f"{p}.auto_delivery", True) and acc_cfg.get("auto_delivery", True):
            self.deliver(o)

    def find_rule(self, o: UOrder) -> Optional[dict]:
        title = (o.title or "").lower()
        for r in self.cfg.get("delivery", []):
            if not r.get("enabled", True) or o.platform not in r.get("platforms", ["funpay", "playerok"]):
                continue
            if r.get("accounts") and o.account not in r["accounts"]:
                continue
            kws = [k.strip().lower() for k in str(r.get("keyword", "")).split("|") if k.strip()]
            if kws and (all(k in title for k in kws) if r.get("match_all") else any(k in title for k in kws)):
                return r
        return None

    def _units(self, o: UOrder, rule) -> int:
        per = int(rule.get("per_unit", 1) or 1)
        return o.amount * per if rule.get("per_amount", True) else per

    def deliver(self, o: UOrder, rule: Optional[dict] = None) -> bool:
        rule = rule or self.find_rule(o)
        if not rule:
            return False
        self.plugins.call("BIND_TO_PRE_DELIVERY", self.cardinal, o, rule)
        mode = rule.get("mode", "file")
        file = rule.get("file", "products.txt")
        product, cost, from_file = "", None, False
        qty = o.amount if rule.get("per_amount", True) else 1
        if mode == "supplier":
            try:
                items, cost = self.suppliers.buy(rule.get("supplier", ""), str(rule.get("product_id", "")), qty, o)
                product = "\n".join(items)
                if cost is None:
                    cost = float(rule.get("cost") or 0) * qty
            except SupplierError as e:
                self.notify("supplier", f"🏭 <b>Поставщик не выдал заказ</b> <code>{esc(o.id)}</code>: {esc(e)}",
                            buttons=[[("📦 Выдать вручную", f"duo:manual:{o.account}:{o.id}", "delivery")]])
                if not rule.get("fallback_file"):
                    return False
                file, mode = rule["fallback_file"], "file"
        if mode == "file" and not product:
            n = self._units(o, rule)
            items = Products.take(file, n)
            if items is None:
                self.notify("delivery", f"⚠️ <b>Нет товара</b> в <code>{esc(file)}</code> для заказа "
                                        f"<code>{esc(o.id)}</code> ({esc(o.title)})")
                if Products.count(file) == 0:
                    self.stock.out_of_stock(file)
                if rule.get("out_of_stock_text"):
                    o.reply(fmt_template(rule["out_of_stock_text"], username=o.buyer, order_id=o.id))
                if o.platform == "funpay" and self.cfg.get("funpay.auto_refund_no_stock"):
                    svc = self.service("funpay", o.account)
                    try:
                        svc.client.refund(o.id)
                        self.stats.inc("refunds")
                        self.stats.set_order_flag(o.id, "refunded", True, "funpay")
                        self.notify("delivery", f"💸 Автовозврат по <code>{esc(o.id)}</code> — нет товара")
                    except Exception as e:
                        log.error("Автовозврат %s: %s", o.id, e)
                return False
            product, from_file = "\n".join(items), True
            cost = float(rule.get("cost") or 0) * qty
        elif mode == "text":
            cost = float(rule.get("cost") or 0) * qty
        left = Products.count(file) if from_file else "∞"
        text = fmt_template(rule.get("text", "$product"), product=product, username=o.buyer, order_id=o.id,
                            order_title=o.title, price=o.price, platform=o.platform, amount=o.amount, left=left)
        if not self.send(o.platform, o.chat_id, text, o.buyer, o.account):
            if from_file:
                Products.add(file, product.splitlines(), front=True)
                msg = "товар возвращён в файл"
            elif mode == "supplier":
                msg = "товар куплен у поставщика — отправьте его вручную (он сохранён)"
            else:
                msg = "отправьте вручную"
            self.notify("delivery", f"❗️ Не удалось отправить заказ <code>{esc(o.id)}</code> — {msg}",
                        buttons=[[("📦 Повторить", f"duo:manual:{o.account}:{o.id}", "delivery")]])
            return False
        self.stats.inc("deliveries")
        self.stats.set_order_flag(o.id, "delivered", True, o.platform)
        if cost:
            self.stats.add_cost(o.id, cost, o.platform)
        elif self.stats.find(o.id, o.platform) and self.stats.find(o.id, o.platform).get("cost") is None \
                and rule.get("cost") is not None:
            self.stats.set_order_flag(o.id, "cost", float(rule.get("cost") or 0), o.platform)
        log.info("📦 Выдан заказ %s (%s/%s), осталось: %s", o.id, o.platform, o.account, left)
        self.notify("delivery", f"📦 Выдан заказ <code>{esc(o.id)}</code> "
                                f"({'FunPay' if o.platform == 'funpay' else 'Playerok'}{self.acc_tag(o.account)}). Осталось: {left}")
        if from_file:
            self.lowstock.check(file)
        if o.platform == "playerok" and self.cfg.get("playerok.mark_sent"):
            svc = self.service("playerok", o.account)
            try:
                svc.bridge.mark_sent(o.id)
            except Exception as e:
                log.warning("Playerok: не отметил отправленным %s: %s", o.id, e)
        self.plugins.call("BIND_TO_POST_DELIVERY", self.cardinal, o, rule)
        return True

    # ═════════ статус ═════════
    def accounts_status(self) -> list:
        out = []
        for platform in ("funpay", "playerok"):
            pool = self.fp if platform == "funpay" else self.pl
            for a in self.cfg.accounts(platform):
                svc = pool.get(a["id"])
                if platform == "funpay":
                    uname = svc.client.username if svc else None
                    bal = svc.client.balance if svc else None
                    cur = svc.client.currency if svc else ""
                else:
                    uname = svc.bridge.username if svc else None
                    bal = svc.bridge.balance() if svc and svc.bridge.ok else None
                    cur = "₽"
                out.append({"id": a["id"], "platform": platform, "name": a.get("name") or uname or a["id"],
                            "username": uname, "enabled": a.get("enabled", True), "running": bool(svc),
                            "ok": bool(svc and svc.ok), "error": svc.error if svc else "", "balance": bal,
                            "currency": cur, "raise": list(svc.raise_info.values()) if svc and platform == "funpay" else []})
        return out

    def status(self) -> dict:
        accs = self.accounts_status()

        def plat(p):
            lst = [a for a in accs if a["platform"] == p]
            run = [a for a in lst if a["running"]]
            bal = [a["balance"] for a in run if isinstance(a["balance"], (int, float))]
            err = next((f"{a['name']}: {a['error']}" for a in run if a["error"] and not a["ok"]), "")
            return {"enabled": bool(run), "ok": bool(run) and all(a["ok"] for a in run), "error": err,
                    "username": ", ".join(a["name"] for a in run) or None, "accounts": len(lst),
                    "balance": round(sum(bal), 2) if bal else None,
                    "currency": run[0]["currency"] if run else "",
                    "raise": [x for a in run for x in a["raise"]]}
        tg = self.tg
        return {
            "version": VERSION,
            "uptime": human_uptime(time.time() - self.stats.started),
            "funpay": plat("funpay"), "playerok": plat("playerok"), "accounts": accs,
            "telegram": {"enabled": bool(tg), "ok": bool(tg and tg.ok), "username": tg.username if tg else None,
                         "error": tg.error if tg else "", "proxy": self.proxy.label() if tg else proxy_label("")},
            "web": {"url": self.public_url, "local": f"http://127.0.0.1:{self.cfg.get('web.port')}"},
            "plugins": len(self.plugins.plugins),
            "products": Products.list(),
            "vacation": bool(self.cfg.get("features.vacation.enabled")),
            "withdraw": {p: {"mode": self.cfg.get(f"withdraw.{p}.mode"), "paused": self.withdraw.paused(p)}
                         for p in ("funpay", "playerok")},
            "supplier_pending": len(self.suppliers.pending()),
            "tunnel": dict(self.web.tunnel) if self.web else {"state": "off"},
            "web_enabled": bool(self.cfg.get("web.enabled")),
        }


def _json(o):
    return json.dumps(o, ensure_ascii=False, default=str)
