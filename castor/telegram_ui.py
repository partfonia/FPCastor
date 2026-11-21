"""Telegram: админ-панель, уведомления, смена бота и прокси на лету, автопереключение прокси."""
from __future__ import annotations

import hmac
import re
import threading
import time
import traceback
from typing import TYPE_CHECKING

import telebot
from telebot import apihelper
from telebot.types import (BotCommand, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM,
                           MenuButtonWebApp, WebAppInfo)

from . import APP_NAME, SIGN, VERSION
from .branding import (WATERMARK_PRESETS, apply_watermark, bot_description, bot_short_description,
                       description_hash, links_html, watermark)
from .compat import CBT, _Anything
from .core import (LOGS_DIR, PROXY_KINDS, Products, UOrder, canonical_proxy, check_proxy, describe_proxy, esc, log,
                   make_backup, parse_proxy, proxy_kind, proxy_label, scheme_title)
from .finance import mask_wallet
from .roles import ROLE_HINTS, ROLE_NAMES, ROLES, perms_of

if TYPE_CHECKING:
    from .app import App


def kb(rows, perms=None) -> "IKM":
    """rows: [[(text, data | url | ('webapp', url) [, право]), ...], ...]
    Кнопки с правом, которого нет в perms, не показываются."""
    m = IKM()
    for row in rows:
        btns = []
        for b in row:
            text, data = b[0], b[1]
            if perms is not None and len(b) > 2 and b[2] and b[2] not in perms:
                continue
            if isinstance(data, tuple) and data[0] == "webapp":
                btns.append(IKB(text, web_app=WebAppInfo(data[1])))
            elif str(data).startswith(("http://", "https://", "tg://")):
                btns.append(IKB(text, url=data))
            else:
                btns.append(IKB(text, callback_data=str(data)[:64]))
        if btns:
            m.row(*btns)
    return m


try:  # pyTelegramBotAPI ≥ 4.15: новый параметр вместо устаревшего disable_web_page_preview
    from telebot.types import LinkPreviewOptions
    NOPREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except Exception:  # pragma: no cover
    NOPREVIEW = {"disable_web_page_preview": True}


def rub(v) -> str:
    """1234.5 → «1 234,5 ₽»"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    n1 = n % 10
    return many if 10 < n < 20 else few if 1 < n1 < 5 else one if n1 == 1 else many


METHOD_NAMES = {"card_rub": "карта ₽", "card_uah": "карта ₴", "card_usd": "карта $", "card_eur": "карта €",
                "qiwi": "QIWI", "yoomoney": "ЮMoney", "webmoney": "WebMoney", "binance": "Binance",
                "trc": "USDT TRC20", "usdt_trc": "USDT TRC20", "sbp": "СБП", "SBP": "СБП"}


def onoff(v):
    return "🟢" if v else "🔴"


NOTIFY_NAMES = {
    "fp_message": "FP сообщения", "fp_order": "FP заказы", "fp_confirm": "FP подтверждения",
    "fp_review": "FP отзывы", "pl_message": "PL сообщения", "pl_deal": "PL сделки",
    "pl_confirm": "PL подтверждения", "pl_review": "PL отзывы", "delivery": "Автовыдача",
    "raise": "Поднятие", "errors": "Ошибки", "start": "Запуск", "low_stock": "Мало товара",
    "reminder": "Напоминания", "report": "Отчёт за день", "stock_out": "Лоты без товара",
    "supplier": "Поставщики", "withdraw": "Вывод средств", "finance": "Финансы",
}

# какое право нужно для callback-действия (duo:<действие>:…)
CB_PERM = {
    "main": None, "lic": "secrets", "licchk": "secrets", "mods": "settings", "mod": "settings", "m2": None, "gsw": "settings", "sec": None, "accs": "settings", "noop": None, "cancel": None, "about": None, "help": None, "set": None, "shops": "settings", "webapp": None,
    "tunnel": "system", "tmode": "system", "ps": None, "wdm": "withdraw", "oos": "delivery", "wm": "settings", "wmp": "settings", "status": "orders", "stats": "finance", "fin": "finance",
    "reply": "chat", "tpl": "chat", "tps": "chat", "cust": "customers",
    "refund": "refund", "refund2": "refund", "plsent": "delivery", "manual": "delivery",
    "dlv": "products", "dadd": "products", "dnew": "delivery", "dtog": "delivery", "ddel": "delivery",
    "stk": "delivery", "sup": "delivery",
    "fp": None, "pl": None, "acc": "settings", "acct": "settings", "accdel": "secrets",
    "accdel2": "secrets", "addacc": "secrets", "raise": "settings", "fpbal": "finance",
    "cmds": "settings", "cdel": "settings", "cntf": "settings", "cadd": "settings",
    "ntf": "settings", "gen": "settings", "feat": "settings", "t": "settings", "e": "settings",
    "plg": "plugins", "pi": "plugins", "ptog": "plugins", "pdel": "plugins", "pdel2": "plugins",
    "prx": "system", "xadd": "system", "xkind": "system", "xkinds": "system", "xtest": "system", "xdel": "system", "xuse": "system",
    "xtoggle": "system", "xall": "system", "xapply": "system", "logs": "system", "restart": "system",
    "backup": "secrets",
    "team": "users", "inv": "users", "usr": "users", "urole": "users", "udel": "users",
    "wd": "withdraw", "wdnow": "withdraw", "wdun": "withdraw",
}
# какие ключи редактирования требуют особых прав
SECRET_EDIT = ("golden_key", "token", "password", "cookies", "api_key", "shared_secret")


class TelegramUI:
    def __init__(self, app: App):
        self.app = app
        self.cfg = app.cfg
        self.ok = False
        self.error = ""
        self.username = None
        self.user_states: dict = {}   # (chat_id, user_id) -> {"state", "data", "msg_id"}
        self._plugin_handlers: list = []   # для повторной регистрации после смены бота
        self._fail_count = 0
        self._health_started = False
        self._sec: dict = {}  # uid → последний открытый раздел площадки
        self._hub: dict = {}
        self._page: dict = {}
        self._ret: dict = {}   # uid → экран, на который вернуться после правки  # uid → страница меню (main / m2), куда возвращает «Назад»  # uid → "fp"/"pl": из какой площадки открыт общий экран (для кнопки «Назад»)
        self.bot = None
        self._build()

    # ---------- создание / перезапуск бота ----------
    def _build(self):
        self.app.proxy.choose()
        apihelper.proxy = self.app.proxy.apihelper_value()
        self.bot = telebot.TeleBot(self.cfg.get("telegram.token"), parse_mode="HTML", threaded=True, num_threads=4)
        self.authorized_users = self.cfg.get("telegram.admins", [])
        self._register()
        for kind, handler, kw in self._plugin_handlers:
            (self.bot.register_message_handler if kind == "msg" else self.bot.register_callback_query_handler)(handler, **kw)

    def _connect(self) -> bool:
        try:
            me = self.bot.get_me()
            self.username, self.ok, self.error = me.username, True, ""
            log.info("🤖 Telegram: @%s (прокси: %s)", me.username, self.app.proxy.label())
            return True
        except Exception as e:
            self.ok, self.error = False, str(e)[:200]
            log.error("🤖 Telegram: не удалось подключиться: %s", e)
            return False

    def start(self):
        if not self._connect() and self.cfg.get("telegram.proxy.failover", True) and self.app.proxy.enabled:
            self._failover()
        self.refresh_commands()
        self.setup_menu_button()
        threading.Thread(target=self.update_description, daemon=True).start()
        bot = self.bot
        threading.Thread(target=self._poll, args=(bot,), name="tg", daemon=True).start()
        if not self._health_started:
            self._health_started = True
            threading.Thread(target=self._health, name="tg-health", daemon=True).start()

    def _poll(self, bot):
        while self.app.running and bot is self.bot:
            try:
                bot.infinity_polling(timeout=30, long_polling_timeout=25, skip_pending=True,
                                     allowed_updates=["message", "callback_query"])
            except Exception as e:
                from .core import _proxy_reason
                why = _proxy_reason(e) if "proxy" in str(e).lower() or "socks" in str(e).lower() else str(e)[:160]
                log.warning("🤖 Telegram: связь прервалась (%s) — переподключаюсь через 5 с", why)
            if bot is self.bot:
                time.sleep(5)

    def _health(self):
        """Раз в минуту проверяет связь с Telegram; при сбоях переключает прокси."""
        while self.app.running:
            time.sleep(60)
            if not self.bot:
                continue
            try:
                self.bot.get_me()
                self.ok, self._fail_count = True, 0
            except Exception as e:
                self._fail_count += 1
                self.ok, self.error = False, str(e)[:200]
                log.warning("🤖 Telegram недоступен (%s): %s", self._fail_count, e)
                if self._fail_count >= 2 and self.cfg.get("telegram.proxy.failover", True) and self.app.proxy.enabled:
                    self._failover()

    def _failover(self):
        old = self.app.proxy.current
        new = self.app.proxy.choose(skip=old, check=True)
        if new != old or not self.ok:
            log.info("🌐 Переключаю прокси Telegram: %s → %s", proxy_label(old), proxy_label(new))
            apihelper.proxy = self.app.proxy.apihelper_value()
            self._fail_count = 0
            if self._connect():
                self.notify(f"🌐 Прокси Telegram переключён: <code>{esc(proxy_label(old))}</code> → "
                            f"<code>{esc(proxy_label(new))}</code>")

    def reload(self) -> tuple:
        """Смена токена/прокси на лету."""
        old_bot = self.bot
        try:
            old_bot.stop_polling()
        except Exception:
            pass
        self.bot = None
        self.ok = False
        time.sleep(1)
        self._build()
        self.start()
        self.register_fallbacks()
        return (True, f"@{self.username} · прокси: {self.app.proxy.label()}") if self.ok else (False, self.error)

    def shutdown(self):
        b, self.bot = self.bot, None
        try:
            if b:
                b.stop_polling()
        except Exception:
            pass

    # ---------- общее ----------
    def is_admin(self, uid) -> bool:
        """Любой участник команды (для совместимости с плагинами Cardinal)."""
        return bool(self.app.team.role(uid))

    def can(self, uid, perm) -> bool:
        return self.app.team.can(uid, perm)

    def perms(self, uid) -> set:
        return perms_of(self.app.team.role(uid))

    def refresh_commands(self):
        cmds = [BotCommand("menu", "🏠 Главное меню"), BotCommand("app", "📱 Открыть панель"),
                BotCommand("status", "📊 Сводка: аккаунты и остатки"), BotCommand("stats", "💰 Выручка и прибыль"),
                BotCommand("raise", "⬆️ Поднять лоты FunPay"), BotCommand("vacation", "🏖 Отпуск вкл/выкл"),
                BotCommand("settings", "⚙️ Настройки"), BotCommand("proxy", "🤖 Бот и прокси"),
                BotCommand("logs", "📜 Файл логов"), BotCommand("restart", "🔄 Перезапуск"),
                BotCommand("help", "❓ Как пользоваться")]
        for c, d in self.app.cardinal.telegram_commands.items():
            cmds.append(BotCommand(c, str(d)[:255]))
        try:
            if self.bot:
                self.bot.set_my_commands(cmds[:100])
        except Exception:
            pass

    def update_description(self):
        """Заполняет «Что умеет этот бот?» и «О себе» — только если текст изменился (лимиты Telegram)."""
        if not self.bot or not self.ok or not self.cfg.get("project.set_bot_description", True):
            return
        key = f"botdesc:{self.username}"
        h = description_hash(self.cfg)
        if self.app.state.get("flags", key) == h:
            return
        try:
            self.bot.set_my_description(bot_description(self.cfg))
            self.bot.set_my_short_description(bot_short_description(self.cfg))
            self.app.state.mark("flags", key, h)
            log.info("🤖 Описание бота обновлено")
        except Exception as e:
            log.warning("Не удалось обновить описание бота: %s", e)

    def setup_menu_button(self):
        url = self.app.public_url
        if url and url.startswith("https://"):
            try:
                self.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("web_app", "📱 Панель", WebAppInfo(url)))
            except Exception as e:
                log.warning("Не удалось поставить кнопку мини-аппа: %s", e)

    def notify(self, text, buttons=None, perm="system", limited=None):
        """Каждому участнику с нужным правом; без права finance — версия без денег; кнопки — по правам."""
        if not self.bot:
            return
        for u in self.app.team.users():
            perms = perms_of(u.get("role"))
            if perm and perm not in perms:
                continue
            body = text if (limited is None or "finance" in perms) else limited
            try:
                self.bot.send_message(u["id"], body, reply_markup=kb(buttons, perms) if buttons else None,
                                      **NOPREVIEW)
            except Exception as e:
                log.debug("TG notify %s: %s", u["id"], e)
        if perm in ("chat", "orders", "products", "delivery"):
            url_only = [[b for b in row if str(b[1]).startswith("http")] for row in (buttons or [])]
            for chat in self.cfg.get("telegram.extra_chats", []):
                try:
                    self.bot.send_message(chat, limited or text, reply_markup=kb(url_only) if any(url_only) else None,
                                          **NOPREVIEW)
                except Exception as e:
                    log.debug("TG notify chat %s: %s", chat, e)

    def edit(self, call, text, rows):
        perms = self.perms(call.from_user.id)
        try:
            self.bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                       reply_markup=kb(rows, perms), **NOPREVIEW)
        except Exception as e:
            if "not modified" not in str(e):
                self.bot.send_message(call.message.chat.id, text, reply_markup=kb(rows, perms),
                                      **NOPREVIEW)

    def send(self, chat_id, text, rows=None, uid=None):
        perms = self.perms(uid or chat_id)
        return self.bot.send_message(chat_id, text, reply_markup=kb(rows, perms) if rows else None,
                                     **NOPREVIEW)

    # ---------- состояния (совместимо с Cardinal) ----------
    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "data": data or {}, "msg_id": message_id}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        s = self.get_state(chat_id, user_id)
        return bool(s and s["state"] == state)

    def clear_state(self, chat_id, user_id, del_msg=False):
        s = self.user_states.pop((chat_id, user_id), None)
        if s and del_msg and s.get("msg_id"):
            try:
                self.bot.delete_message(chat_id, s["msg_id"])
            except Exception:
                pass
        return s

    # ---------- API для плагинов Cardinal ----------
    def msg_handler(self, handler, **kwargs):
        func = kwargs.pop("func", None)
        user_func = func

        def guard(m):
            return self.is_admin(m.from_user.id) and (user_func(m) if user_func else True)
        self._plugin_handlers.append(("msg", handler, dict(kwargs, func=guard)))
        self.bot.register_message_handler(handler, func=guard, **kwargs)

    def cbq_handler(self, handler, func=None, **kwargs):
        def guard(c):
            return self.is_admin(c.from_user.id) and (func(c) if func else True)
        self._plugin_handlers.append(("cbq", handler, dict(kwargs, func=guard)))
        self.bot.register_callback_query_handler(handler, func=guard, **kwargs)

    def file_handler(self, state, handler):
        kw = dict(content_types=["document"],
                  func=lambda m: self.is_admin(m.from_user.id) and self.check_state(m.chat.id, m.from_user.id, state))
        self._plugin_handlers.append(("msg", handler, kw))
        self.bot.register_message_handler(handler, **kw)

    def inline_handler(self, *a, **k):
        pass

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Anything()

    # ---------- экраны ----------
    # ═══════════════ меню в стиле Cardinal: разделы по одному в строке, две страницы ═══════════════
    def _status_line(self, uid) -> str:
        st = self.app.status()
        fin = "finance" in self.perms(uid)
        parts = []
        for p, name in (("funpay", "FunPay"), ("playerok", "Playerok")):
            accs = [a for a in st["accounts"] if a["platform"] == p]
            if not accs:
                parts.append(f"⚪️ {name}: нет аккаунта")
                continue
            for a in accs:
                dot = "⚪️" if not a["running"] else ("🟢" if a["ok"] else "🔴")
                parts.append(f"{dot} {name}: {esc(a['name'])}")
        t = self.app.stats.today()
        n = t["funpay"]["count"] + t["playerok"]["count"]
        today = f"📈 Сегодня: {n} {plural(n, 'заказ', 'заказа', 'заказов')}" + (
            f" · {rub(t['total']['sum'])}" if fin else "")
        line = "\n".join(parts) + "\n" + today
        if st["vacation"]:
            line += "\n🏖 Включён режим отпуска"
        bad = [a for a in st["accounts"] if a["running"] and not a["ok"]]
        if bad:
            line += "\n⚠️ " + esc(bad[0]["error"] or "нет связи с площадкой")[:120]
        return line

    def main_menu(self, uid):
        self._page[uid] = "main"
        text = (f"<b>{SIGN} Castor</b> v{VERSION}\n\n{self._status_line(uid)}\n\n"
                f"<i>Выберите раздел 👇</i>")
        url = self.app.public_url
        rows = []
        if url.startswith("https://"):
            rows.append([("📱 Открыть панель", ("webapp", url))])
        rows += [[("⚙️ Глобальные переключатели", "duo:gsw", "settings")],
                 [("🔔 Уведомления", "duo:ntf", "settings")],
                 [("🤖 Автоответчик", "duo:cmds", "settings")],
                 [("📦 Автовыдача", "duo:dlv", "products")],
                 [("👋 Приветствие", "duo:sec:greet", "settings")],
                 [("✅ Ответ на подтверждение", "duo:sec:confirm", "settings")],
                 [("⭐ Ответ на отзывы", "duo:sec:reviews", "settings")],
                 [("📊 Статистика", "duo:fin", "finance")],
                 [("🧩 Модули: аренда, коды с почты, автоцена…", "duo:mods", "settings")],
                 [("Далее ▶️", "duo:m2")]]
        return text, rows

    def menu2(self, uid):
        self._page[uid] = "m2"
        text = f"<b>{SIGN} Castor</b> — ещё разделы\n\n{self._status_line(uid)}"
        rows = [[("👤 Аккаунты FunPay и Playerok", "duo:accs", "settings")],
                [("💸 Автовывод средств", "duo:sec:wd", "withdraw")],
                [("🏷 Лоты без товара", "duo:sec:lots", "delivery")],
                [("📝 Шаблоны ответов", "duo:sec:tpl", "settings")],
                [("🚫 Чёрный список", "duo:sec:bl", "settings")],
                [("🕘 Рабочие часы и отпуск", "duo:sec:hours", "settings")],
                [("✍️ Подпись в чатах", "duo:wm", "settings")],
                [("🌐 Бот и прокси", "duo:prx", "system")],
                [("📱 Доступ к панели", "duo:webapp", "system")],
                [("👨‍💼 Команда", "duo:team", "users")],
                [("🧩 Плагины", "duo:plg:0", "plugins")],
                [("🔑 Лицензии платных плагинов", "duo:lic", "secrets")],
                [("🗳 Логи", "duo:logs", "system"), ("💾 Бекап", "duo:backup", "secrets"),
                 ("🔄 Перезапуск", "duo:restart", "system")],
                [("◀️ Назад", "duo:main:1")]]
        return text, rows

    def _home(self, uid) -> str:
        """Куда ведёт «Назад» из раздела — на ту страницу меню, с которой пришли."""
        return "duo:m2" if self._page.get(uid) == "m2" else "duo:main:1"

    def gsw_menu(self):
        """Глобальные переключатели: одна строка — одна функция, справа FunPay и Playerok."""
        c = self.cfg
        both = lambda key: [(f"🔵 {'вкл' if c.get('funpay.' + key) else 'выкл'}", f"duo:t:funpay.{key}:gsw", "settings"),  # noqa: E731
                            (f"🟢 {'вкл' if c.get('playerok.' + key) else 'выкл'}", f"duo:t:playerok.{key}:gsw", "settings")]
        one = lambda path, p="fp": [(f"{'🔵' if p == 'fp' else '🟢'} {'вкл' if c.get(path) else 'выкл'}",  # noqa: E731
                                     f"duo:t:{path}:gsw", "settings")]
        glob = lambda path: [(f"{'🟢 вкл' if c.get(path) else '🔴 выкл'}", f"duo:t:{path}:gsw", "settings")]  # noqa: E731
        lbl = lambda t: (t, "duo:noop")  # noqa: E731
        text = ("<b>⚙️ Глобальные переключатели</b>\n\n"
                "Нажмите на 🔵 FunPay или 🟢 Playerok, чтобы включить или выключить функцию. Применяется сразу.")
        rows = [[lbl("Площадка работает")] + both("enabled"),
                [lbl("Автовыдача")] + both("auto_delivery"),
                [lbl("Автоответчик")] + both("auto_reply"),
                [lbl("Приветствие")] + both("greeting.enabled"),
                [lbl("Ответ на подтверждение")] + both("confirm_reply.enabled"),
                [lbl("Текст при заказе")] + both("order_text.enabled"),
                [lbl("Автоподнятие лотов")] + one("funpay.auto_raise.enabled"),
                [lbl("Ответ на отзывы")] + one("funpay.review_reply.enabled"),
                [lbl("Автовозврат без товара")] + one("funpay.auto_refund_no_stock"),
                [lbl("Отмечать «отправлено»")] + one("playerok.mark_sent", "pl"),
                [lbl("Режим отпуска")] + glob("features.vacation.enabled"),
                [lbl("Нерабочее время")] + glob("general.work_hours.enabled"),
                [lbl("Подпись в чатах")] + glob("general.watermark_enabled"),
                [("◀️ Назад", "duo:main:1")]]
        return text, rows

    def section(self, name, uid):
        """Разделы с текстами: один экран — одна тема."""
        c = self.cfg
        back = [("◀️ Назад", self._home(uid))]
        q = lambda v, n=300: f"<blockquote>{esc(str(v or '—'))[:n]}</blockquote>"  # noqa: E731
        sw = lambda path, label: (f"{'🟢' if c.get(path) else '🔴'} {label}", f"duo:t:{path}:sec-{name}", "settings")  # noqa: E731
        ed = lambda path, label, perm="settings": (f"✏️ {label}", f"duo:e:{path}", perm)  # noqa: E731
        vars_ = "<i>Можно использовать: $username, $order_id, $order_title</i>"

        if name == "greet":
            text = ("<b>👋 Приветствие</b>\n\nПервое сообщение новому покупателю — не чаще раза в "
                    f"{c.get('funpay.greeting.cooldown_days')} дн. одному человеку.\n\n"
                    f"<b>🔵 FunPay</b>\n{q(c.get('funpay.greeting.text'))}"
                    f"<b>🟢 Playerok</b>\n{q(c.get('playerok.greeting.text'))}"
                    f"<b>⭐ Постоянным покупателям</b>\n{q(c.get('features.returning_buyer.text'))}" + vars_)
            rows = [[sw("funpay.greeting.enabled", "FunPay"), sw("playerok.greeting.enabled", "Playerok")],
                    [ed("funpay.greeting.text", "Текст FunPay")], [ed("playerok.greeting.text", "Текст Playerok")],
                    [sw("features.returning_buyer.enabled", "Постоянным — другой текст")],
                    [ed("features.returning_buyer.text", "Текст постоянным")],
                    [ed("funpay.greeting.cooldown_days", "Раз в сколько дней")], back]
            return text, rows

        if name == "confirm":
            text = ("<b>✅ Ответ на подтверждение</b>\n\nСообщение, когда покупатель подтвердил заказ.\n\n"
                    f"<b>🔵 FunPay</b>\n{q(c.get('funpay.confirm_reply.text'))}"
                    f"<b>🟢 Playerok</b>\n{q(c.get('playerok.confirm_reply.text'))}"
                    f"<b>⏰ Напоминание подтвердить</b> — через {c.get('features.confirm_reminder.after_hours')} ч\n"
                    f"{q(c.get('features.confirm_reminder.text'))}" + vars_)
            rows = [[sw("funpay.confirm_reply.enabled", "FunPay"), sw("playerok.confirm_reply.enabled", "Playerok")],
                    [ed("funpay.confirm_reply.text", "Текст FunPay")], [ed("playerok.confirm_reply.text", "Текст Playerok")],
                    [sw("features.confirm_reminder.enabled", "Напоминать подтвердить")],
                    [ed("features.confirm_reminder.text", "Текст напоминания")],
                    [ed("features.confirm_reminder.after_hours", "Через сколько часов")], back]
            return text, rows

        if name == "reviews":
            texts = c.get("funpay.review_reply.texts", {})
            text = "<b>⭐ Ответ на отзывы</b> (FunPay)\n\nБот отвечает на отзыв своим текстом для каждой оценки.\n\n"
            for st in ("5", "4", "3", "2", "1"):
                text += f"{'⭐' * int(st)}\n{q(texts.get(st), 150)}"
            rows = [[sw("funpay.review_reply.enabled", "Отвечать на отзывы")]]
            rows += [[ed(f"funpay.review_reply.texts.{st}", f"Текст на {st} ⭐")] for st in ("5", "4", "3", "2", "1")]
            return text, rows + [back]

        if name == "hours":
            text = (f"<b>🕘 Рабочие часы и отпуск</b>\n\n"
                    f"<b>Нерабочее время</b> {c.get('general.work_hours.from')}–{c.get('general.work_hours.to')} — "
                    f"бот отвечает покупателям этим текстом:\n{q(c.get('general.work_hours.away_text'))}"
                    f"<b>Отпуск</b>" + (f" до {esc(c.get('features.vacation.until'))}" if c.get("features.vacation.until") else "")
                    + f"\n{q(c.get('features.vacation.text'))}<i>Автовыдача работает в любом режиме.</i>")
            rows = [[sw("general.work_hours.enabled", "Нерабочее время")],
                    [ed("general.work_hours.range", "Часы работы")], [ed("general.work_hours.away_text", "Текст нерабочего времени")],
                    [sw("features.vacation.enabled", "Режим отпуска")],
                    [ed("features.vacation.text", "Текст отпуска")], [ed("features.vacation.until", "Отпуск до даты")], back]
            return text, rows

        if name == "tpl":
            tl = c.get("templates", [])
            text = ("<b>📝 Шаблоны ответов</b>\n\nПоявляются кнопкой «📝» под сообщением покупателя — ответить в одно касание.\n\n"
                    + ("".join(f"{i + 1}. {esc(t)[:120]}\n" for i, t in enumerate(tl)) or "Шаблонов пока нет.\n"))
            rows = [[("✏️ Изменить шаблоны", "duo:e:templates", "settings")], back]
            return text, rows

        if name == "bl":
            bl = c.get("blacklist.users", [])
            text = ("<b>🚫 Чёрный список</b>\n\nЭтим покупателям бот не отвечает и не выдаёт товар автоматически.\n\n"
                    + (", ".join(f"<code>{esc(u)}</code>" for u in bl) or "Список пуст."))
            rows = [[("➕ Добавить", "duo:e:blacklist.add", "settings")],
                    [("➖ Убрать", "duo:e:blacklist.remove", "settings")], back]
            return text, rows

        if name in ("wd", "lots"):
            t, r = self._platform_section("fp", name, uid)
            if name == "wd":  # автовывод — для каждой площадки свой экран
                t2, r2 = self._platform_section("pl", "wd", uid)
                note = "\n\n<i>Реквизиты указываются в 📱 приложении — так безопаснее.</i>"
                text = t.replace("🔵 FunPay · 💸 Автовывод", "💸 Автовывод · 🔵 FunPay").replace(note, "") + "\n\n" + \
                    t2.replace("🟢 Playerok · 💸 Автовывод", "🟢 Playerok")
                rows = [x for x in r[:-1]] + [[("━━  🟢 Playerok  ━━", "duo:noop")]] + [x for x in r2[:-1]] + [back]
                rows = [[(b[0], b[1].replace(":fp-wd", ":sec-wd").replace(":pl-wd", ":sec-wd"), *b[2:]) for b in row]
                        for row in rows]
                return text, rows
            t = t.replace("🔵 FunPay · ", "")
            r = [[(b[0], b[1].replace(":fp-lots", ":sec-lots"), *b[2:]) for b in row] for row in r[:-1]] + [back]
            return t, r
        return "Раздел не найден", [back]

    # ═══════════════ встроенные модули ═══════════════
    MOD_GROUPS = ("Выдача", "Перепродажа", "Общение", "Лоты и цены", "Заказы", "Деньги", "Безопасность", "Прочее")

    def mods_menu(self):
        mods = self.app.modules.describe()
        on = sum(1 for d in mods if d["enabled"])
        text = (f"<b>🧩 Модули</b>\n\nДополнительные функции — как плагины, но уже встроены. "
                f"Включено {on} из {len(mods)}.\nНажмите на модуль: описание, настройки, включение.")
        rows = []
        groups = list(self.MOD_GROUPS) + sorted({d["group"] for d in mods} - set(self.MOD_GROUPS))
        for g in groups:
            items = [d for d in mods if d["group"] == g]
            if not items:
                continue
            rows.append([(f"━━  {g}  ━━", "duo:noop")])
            for k in range(0, len(items), 2):
                rows.append([(f"{d['icon']} {d['name']}{' ✅' if d['enabled'] else ''}", f"duo:mod:{d['key']}", "settings")
                             for d in items[k:k + 2]])
        rows.append([("🔌 Свои плагины", "duo:plg:0", "plugins")])
        rows.append([("◀️ Назад", "duo:main")])
        return text, rows

    def mod_card(self, key):
        m = self.app.modules.get(key)
        if not m:
            return "Модуль не найден", [[("◀️ Модули", "duo:mods")]]
        cfg = m.cfg
        text = (f"<b>{m.ICON} {esc(m.NAME)}</b> {'🟢 включён' if m.enabled else '⚪️ выключен'}\n\n{esc(m.DESCRIPTION)}\n")
        st = self.app.modules._safe(m, "status")
        if st:
            text += f"\n📊 <b>Сейчас:</b> {esc(st)}\n"
        rows = [[(f"{'🔴 Выключить' if m.enabled else '🟢 Включить'} модуль", f"duo:t:modules.{key}.enabled:mod-{key}", "settings")]]
        items_note = []
        for f in m.FIELDS:
            path, label, typ = f[0], f[1], f[2]
            full = f"modules.{key}.{path}"
            val = cfg.get(path)
            if typ == "bool":
                rows.append([(f"{'🟢' if val else '🔴'} {label}", f"duo:t:{full}:mod-{key}", "settings")])
            elif typ == "select":
                opts = f[3]
                cur = next((n for v, n in opts if v == val), val)
                rows.append([(f"🔁 {label}: {cur}", f"duo:mod:{key}:sel:{path}", "settings")])
            elif typ in ("text", "area", "number"):
                shown = str(val if val is not None else "")
                if typ != "area":
                    text += f"・ {esc(label)}: <code>{esc(shown[:60])}</code>\n"
                rows.append([(f"✏️ {label}", f"duo:e:{full}", "settings")])
            elif typ == "secret":
                rows.append([(f"🔑 {label}: {'сохранён' if val else 'не задан'}", f"duo:e:{full}", "secrets")])
            elif typ in ("items", "lines"):
                n = len(val or [])
                items_note.append(f"{label}: {n}")
        if items_note:
            text += "\n<b>Списки</b> (удобнее настраивать в 📱 панели → Настройки → Модули):\n" + \
                "\n".join(f"・ {esc(x)}" for x in items_note) + "\n"
            url = self.app.public_url
            if url.startswith("https://"):
                rows.append([("📱 Настроить в панели", ("webapp", url))])
        for aid, label in (self.app.modules._safe(m, "actions") or []):
            rows.append([(label, f"duo:mod:{key}:act:{aid}"[:64], "settings")])
        rows.append([("◀️ Модули", "duo:mods")])
        return text, rows

    def lic_menu(self):
        from . import licensing
        items = licensing.describe()
        text = "<b>🔑 Лицензии</b>\n\n"
        if not items:
            text += ("Платных плагинов пока нет. Купленный плагин присылайте боту файлом или кладите в папку plugins, "
                     "потом вводите ключ здесь.")
        rows = []
        for d in items:
            text += f"{'🟢' if d['ok'] else '🔴'} <b>{esc(d['name'])}</b>\n<i>{esc(d['status'])}</i>\n\n"
            row = [(f"✏️ Ключ: {d['name'][:24]}", f"duo:e:licenses.{d['id']}", "secrets")]
            if d.get("shop"):
                row.append(("🛒 Купить / продлить", d["shop"]))
            rows.append(row)
        if items:
            rows.append([("🔄 Проверить ключи", "duo:licchk", "secrets")])
        rows.append([("◀️ Назад", "duo:m2")])
        return text, rows

    def accs_menu(self, uid):
        c = self.cfg
        text = "<b>👤 Аккаунты</b>\n\nНажмите на аккаунт: включить, прокси, ключ, автовыдача.\n"
        rows = []
        for p, platform, name in (("fp", "funpay", "🔵 FunPay"), ("pl", "playerok", "🟢 Playerok")):
            pool = self.app.fp if p == "fp" else self.app.pl
            text += f"\n<b>{name}</b>\n"
            accs = c.accounts(platform)
            if not accs:
                text += "   нет аккаунтов\n"
            for a in accs:
                svc = pool.get(a["id"])
                dot = "⚪️" if not a.get("enabled", True) else ("🟢" if svc and svc.ok else "🔴")
                text += f"   {dot} {esc(self.app.account_name(a['id']))} · 🌐 {esc(proxy_label(a.get('proxy', '')))}\n"
                rows.append([(f"{name.split()[0]} {self.app.account_name(a['id'])[:30]}", f"duo:acc:{a['id']}")])
            rows.append([(f"➕ Добавить аккаунт {name[2:]}", f"duo:addacc:{p}", "secrets")])
        rows.append([("◀️ Назад", self._home(uid))])
        return text, rows

    def settings_menu(self, uid):
        perms = self.perms(uid)
        text = ("<b>⚙️ Настройки</b>\n\n"
                "✨ <b>Функции</b> — напоминания, отчёт за день, антифлуд, автобекап\n"
                "🔔 <b>Уведомления</b> — о чём писать вам в Telegram\n"
                "🕘 <b>Работа</b> — рабочие часы, шаблоны, чёрный список\n"
                "✍️ <b>Подпись</b> — водяной знак в сообщениях покупателям\n"
                "🤖 <b>Бот и прокси</b> — сменить бота, прокси для Telegram\n"
                "📱 <b>Мини-апп</b> — работает ли панель, новый адрес\n"
                "🧩 <b>Плагины</b> · 👨‍💼 <b>Команда</b> — помощники и их права")
        rows = [[("✨ Функции", "duo:feat", "settings"), ("🔔 Уведомления", "duo:ntf", "settings")],
                [("🕘 Работа", "duo:gen", "settings"), ("✍️ Подпись", "duo:wm", "settings")],
                [("🤖 Бот и прокси", "duo:prx", "system"), ("📱 Мини-апп", "duo:webapp")],
                [("🧩 Плагины", "duo:plg:0", "plugins"), ("👨‍💼 Команда", "duo:team", "users")],
                [("💾 Бекап", "duo:backup", "secrets"), ("📜 Логи", "duo:logs", "system"),
                 ("🔄 Перезапуск", "duo:restart", "system")],
                [("ℹ️ О боте", "duo:about"), ("🏠 Меню", "duo:main")]]
        if not any(len(r) > 2 and r[2] in perms for row in rows for r in row):
            text = "<b>⚙️ Настройки</b>\n\nУ вашей роли нет доступа к настройкам."
        return text, rows

    def shops_menu(self):
        st = self.app.status()
        text = "<b>🏪 Магазины</b>\n"
        for p, name, icon in (("funpay", "FunPay", "🔵"), ("playerok", "Playerok", "🟢")):
            accs = [a for a in st["accounts"] if a["platform"] == p]
            on = self.cfg.get(f"{p}.enabled")
            text += f"\n{icon} <b>{name}</b>" + ("" if on else "  ·  <i>выключен</i>") + "\n"
            if not accs:
                text += "   аккаунтов нет\n"
            for a in accs:
                dot = "⚪️" if not a["running"] else ("🟢" if a["ok"] else "🔴")
                text += f"   {dot} {esc(a['name'])}" + (f" — {esc(a['error'])}" if a["error"] and not a["ok"] else "") + "\n"
        raise_txt = [x for a in st["accounts"] for x in a.get("raise", [])]
        if raise_txt:
            text += "\n<b>⬆️ Поднятие</b>\n" + esc("\n".join(raise_txt[:4]))
        text += "\n\n<i>Откройте площадку, чтобы добавить аккаунт или настроить автоответ, приветствие и выдачу.</i>"
        rows = [[("🔵 FunPay", "duo:fp"), ("🟢 Playerok", "duo:pl")],
                [("⬆️ Поднять лоты", "duo:raise"), ("💰 Балансы", "duo:fpbal", "finance")],
                [("🏠 Главное меню", "duo:main")]]
        return text, rows

    def help_menu(self):
        text = (f"<b>❓ Как пользоваться</b>\n\n"
                "<b>/menu</b> — меню из двух страниц, как в Cardinal:\n"
                "• <b>Глобальные переключатели</b> — всё вкл/выкл на одном экране, отдельно для 🔵 FunPay и 🟢 Playerok\n"
                "• <b>Уведомления, Автоответчик, Автовыдача, Приветствие, Ответ на подтверждение, Ответ на отзывы, Статистика</b>\n"
                "• <b>Далее ▶️</b> — аккаунты, автовывод, лоты без товара, шаблоны, чёрный список, рабочие часы, "
                "подпись, бот и прокси, панель, команда, плагины, логи\n\n"
                "📱 <b>Панель</b> — то же самое в удобном окне: кнопка «Панель» слева от поля ввода.\n\n"
                "Если панель не открывается — /app.\n\n"
                "<b>Команды</b>\n"
                "/menu — главное меню\n/status — сводка\n/app — открыть панель\n"
                "/raise — поднять лоты FunPay\n/vacation — отпуск вкл/выкл\n"
                "/proxy — бот и прокси\n/logs — файл логов\n/restart — перезапуск")
        lk = links_html(self.cfg, esc)
        if lk:
            text += f"\n\n{lk}"
        return text, [[("🏠 Главное меню", "duo:main")]]

    def webapp_menu(self):
        from .tunnel import MODES, PROVIDER_NAMES
        web = self.app.web
        tm = web.tm if web else None
        url = self.app.public_url
        info = tm.info if tm else {"state": "off", "error": "", "provider": ""}
        mode = self.cfg.get("web.tunnel", "none")
        local = f"http://127.0.0.1:{self.cfg.get('web.port')}"
        state = {"ok": "🟢 Работает", "starting": "🟡 Подключаюсь…", "unreachable": "🔴 Адрес не открывается",
                 "failed": "🔴 Не получилось подключиться", "off": "⚪️ Выключено"}.get(info["state"], info["state"])
        if info["state"] == "off" and url:
            state = "🔗 Свой адрес"
        prov = PROVIDER_NAMES.get(info.get("provider"), "")
        text = (f"<b>📱 Приложение</b>\n\n{state}" + (f" · через {prov}" if prov and info["state"] != "off" else "")
                + (f"\n<i>{esc(info['error'])}</i>" if info.get("error") and info["state"] != "ok" else "")
                + f"\n\n・ 🔀 Способ: <b>{esc(MODES.get(mode, mode))}</b>"
                + (f"\n・ 🌐 Адрес: <code>{esc(url)}</code>" if url else "")
                + f"\n・ 💻 На этом компьютере: <code>{local}</code>")
        if tm and tm.attempts:
            text += "\n\n<b>Последние попытки</b>\n" + "\n".join(
                f"・ {time.strftime('%H:%M', time.localtime(t))} {esc(n)}: {esc(r)[:70]}" for t, n, r in list(tm.attempts)[-5:])
        if info["state"] in ("unreachable", "failed"):
            text += ("\n\n<b>Что можно сделать</b>\n"
                     "1. «🔁 Другой способ» — бот попробует следующий сервис.\n"
                     "2. Включите прокси для Telegram-бота: бот пустит через него и туннель.\n"
                     "3. На телефоне включите VPN — некоторые адреса провайдеры режут.")
        text += "\n\n<i>Адрес меняется при перезапуске — открывайте приложение кнопкой «📱 Панель» или из /menu.</i>"
        rows = []
        if url.startswith("https://"):
            rows.append([("📱 Открыть приложение", ("webapp", url))])
        rows += [[("🔄 Проверить", "duo:tunnel:check", "system"), ("🔁 Другой способ", "duo:tunnel:restart", "system")],
                 [(("• " if mode == m else "") + n.split(" —")[0].split(" (")[0], f"duo:tmode:{m}", "system")
                  for m, n in MODES.items() if m in ("auto", "cloudflared", "localhostrun", "pinggy")],
                 [(("• " if mode == "none" else "") + "Выключить", "duo:tmode:none", "system"),
                  ("🔗 Свой адрес", "duo:e:web.public_url", "system")],
                 [("🔐 Пароль панели", "duo:e:web.password", "secrets"), ("◀️ Назад", "duo:main")]]
        return text, rows

    def about_menu(self):
        lk = links_html(self.cfg, esc)
        text = (f"<b>{SIGN} {APP_NAME}</b> v{VERSION}\n\nБот-помощник продавца: FunPay и Playerok в одном боте."
                + (f"\n\n{lk}" if lk else ""))
        rows = [[("🔄 Канал обновлений", "duo:e:project.links.updates", "settings"),
                 ("🧩 Плагины", "duo:e:project.links.plugins", "settings")],
                [("💬 Чат", "duo:e:project.links.chat", "settings"), ("💰 Донат", "duo:e:project.links.donate", "settings")],
                [("🛠 GitHub", "duo:e:project.links.github", "settings"),
                 (f"{onoff(self.cfg.get('project.set_bot_description', True))} Описание бота",
                  "duo:t:project.set_bot_description:about", "settings")],
                [("◀️ Назад", "duo:main")]]
        return text, rows

    def watermark_menu(self):
        g = self.cfg.get("general", {})
        wm = watermark(self.cfg)
        preview = apply_watermark(self.cfg, "Спасибо за покупку! Ваш ключ: XXXX-XXXX")
        text = (f"<b>✍️ Подпись в сообщениях покупателям</b>\n"
                f"Бот ставит её перед каждым своим сообщением в чате FunPay и Playerok — как Кардинал.\n\n"
                f"Сейчас: {'<b>' + esc(wm) + '</b>' if wm else 'выключена'}\n\nТак увидит покупатель:\n"
                f"<blockquote>{esc(preview)}</blockquote>")
        rows = [[(f"{onoff(g.get('watermark_enabled', True))} Подпись", "duo:t:general.watermark_enabled:wm"),
                 (f"{onoff(g.get('watermark_newline', True))} Отдельной строкой", "duo:t:general.watermark_newline:wm")],
                [(f"{onoff(g.get('watermark_manual', True))} И в ответах из Telegram/мини-аппа", "duo:t:general.watermark_manual:wm")]]
        rows += [[(p, f"duo:wmp:{i}")] for i, p in enumerate(WATERMARK_PRESETS)]
        rows += [[("✏️ Своя подпись", "duo:e:general.watermark")], [("◀️ Назад", "duo:main")]]
        return text, rows

    # ---------- площадки и аккаунты ----------
    # ═══════════════ площадка: главный экран и разделы ═══════════════
    PSECTIONS = {
        "profile": "👤 Профиль", "stats": "📊 Статистика", "chats": "💬 Чаты", "orders": "🛒 Заказы",
        "greet": "👋 Приветствие", "cmds": "❗ Команды", "dlv": "🚀 Автовыдача", "raise": "⬆️ Поднятие",
        "confirm": "☑️ Подтверждение", "reviews": "⭐ Отзывы", "wd": "💸 Автовывод", "lots": "🏷 Нет товара",
        "accs": "🔑 Аккаунты и прокси", "ntf": "🔔 Уведомления",
    }

    def _pinfo(self, p):
        platform = "funpay" if p == "fp" else "playerok"
        return platform, ("🔵 FunPay" if p == "fp" else "🟢 Playerok"), (self.app.fp if p == "fp" else self.app.pl)

    @staticmethod
    def _ago(ts) -> str:
        d = max(0, int(time.time() - (ts or 0)))
        if d < 60:
            return "только что"
        if d < 3600:
            return f"{d // 60} мин назад"
        if d < 86400:
            return f"{d // 3600} ч назад"
        return time.strftime("%d.%m %H:%M", time.localtime(ts))

    def platform_menu(self, p, uid=None):
        platform, title, pool = self._pinfo(p)
        c = self.cfg
        perms = self.perms(uid) if uid else perms_of("owner")
        fin = "finance" in perms
        st = self.app.status()
        accs = [a for a in st["accounts"] if a["platform"] == platform]
        run = [a for a in accs if a["running"]]
        on = c.get(f"{platform}.enabled")
        if not on:
            state = "⚪️ Выключено"
        elif not accs:
            state = "🟡 Нет аккаунтов — добавьте в «🔑 Аккаунты и прокси»"
        elif run and all(a["ok"] for a in run):
            state = "🟢 Работает"
        else:
            state = "🔴 Есть проблемы"
        text = f"<b>{title}</b>\n{state}\n"
        for a in accs:
            dot = "⚪️" if not a["running"] else ("🟢" if a["ok"] else "🔴")
            bal = f" · <b>{rub(a['balance'])}</b>" if fin and a["running"] and isinstance(a["balance"], (int, float)) else ""
            text += f"\n{dot} {esc(a['name'])}{bal}" + (f"\n    <i>{esc(a['error'])}</i>" if a["error"] and not a["ok"] else "")
        t = self.app.stats.today()[platform]
        text += f"\n\n📈 <b>Сегодня:</b> {t['count']} {plural(t['count'], 'заказ', 'заказа', 'заказов')}"
        if fin:
            text += f" · {rub(t['sum'])} · прибыль {rub(t['profit'])}"
        flags = [("автовыдача", c.get(f"{platform}.auto_delivery")), ("автоответ", c.get(f"{platform}.auto_reply")),
                 ("приветствие", c.get(f"{platform}.greeting.enabled"))]
        if p == "fp":
            flags.append(("поднятие", c.get("funpay.auto_raise.enabled")))
        text += "\n⚙️ " + " · ".join(f"{'✅' if v else '❌'} {n}" for n, v in flags)
        if c.get("features.vacation.enabled"):
            text += "\n🏖 <b>Режим отпуска</b>"

        S = self.PSECTIONS
        sec = lambda k, perm=None: (S[k], f"duo:ps:{p}:{k}", perm)  # noqa: E731
        rows = [[("━━━  📋 Управление  ━━━", "duo:noop")],
                [sec("profile", "orders"), sec("stats", "orders")],
                [sec("chats", "chat"), sec("orders", "orders")],
                [("━━━  ⚙️ Автоматизация  ━━━", "duo:noop", "settings")],
                [sec("greet", "settings"), sec("cmds", "settings")],
                [sec("dlv", "products"), sec("raise", "settings") if p == "fp" else sec("lots", "delivery")],
                [sec("confirm", "settings"), sec("reviews", "settings") if p == "fp" else sec("wd", "withdraw")]]
        if p == "fp":
            rows.append([sec("wd", "withdraw"), sec("lots", "delivery")])
        rows += [[("━━━  🔧 Прочее  ━━━", "duo:noop")],
                 [sec("accs", "settings"), sec("ntf", "settings")],
                 [(f"{'🟢 ' + title[2:] + ' включён' if on else '🔴 ' + title[2:] + ' выключен'}",
                   f"duo:t:{platform}.enabled:{p}", "settings")],
                 [("🏠 Меню", "duo:main"), ("🔄 Обновить", f"duo:{p}")]]
        return text, rows

    def platform_section(self, p, sec, uid=None):
        """Кнопки редактирования без явного права требуют «settings» — у кого его нет, их не увидит."""
        text, rows = self._platform_section(p, sec, uid)
        fix = lambda b: b if len(b) > 2 or not str(b[1]).startswith("duo:e:") else (b[0], b[1], "settings")  # noqa: E731
        return text, [[fix(b) for b in row] for row in rows]

    def _platform_section(self, p, sec, uid=None):
        platform, title, pool = self._pinfo(p)
        c, k = self.cfg, platform
        perms = self.perms(uid) if uid else perms_of("owner")
        fin = "finance" in perms
        back = [("⬅️ Назад", f"duo:{p}"), ("🔄 Обновить", f"duo:ps:{p}:{sec}")]
        tg = lambda path, label: (f"{onoff(c.get(path))} {label}", f"duo:t:{path}:{p}-{sec}", "settings")  # noqa: E731
        ed = lambda path, label: (label, f"duo:e:{path}", "settings")  # noqa: E731
        head = f"<b>{title} · {self.PSECTIONS.get(sec, sec)}</b>\n\n"
        q = lambda v, n=180: f"<blockquote>{esc(str(v or '—'))[:n]}</blockquote>"  # noqa: E731

        if sec == "profile":
            st = self.app.status()
            accs = [a for a in st["accounts"] if a["platform"] == platform]
            orders = [o for o in self.app.stats.data["orders"] if o.get("p") == platform]
            text = head
            for a in accs:
                svc = pool.get(a["id"])
                dot = "⚪️ выключен" if not a["running"] else ("🟢 работает" if a["ok"] else "🔴 " + esc(a["error"] or "нет связи"))
                mine = [o for o in orders if o.get("a") == a["id"]]
                text += (f"<b>{esc(a['name'])}</b>\n"
                         f"・ 🆔 Логин: {esc(a.get('username') or '—')}\n"
                         f"・ 💡 Состояние: {dot}\n")
                if fin and isinstance(a["balance"], (int, float)):
                    text += f"・ 💰 Баланс: <b>{rub(a['balance'])}</b>\n"
                text += f"・ 🛒 Продаж через бота: {len(mine)}\n"
                if p == "fp" and svc:
                    ri = list(getattr(svc, "raise_info", {}).values())
                    if ri:
                        text += f"・ ⬆️ Поднятие: {esc(ri[-1])[:80]}\n"
                text += "\n"
            if not accs:
                text += "Аккаунтов нет. Добавьте в «🔑 Аккаунты и прокси».\n"
            rows = [[("🔑 Аккаунты и прокси", f"duo:ps:{p}:accs", "settings")], back]
            return text, rows

        if sec == "stats":
            s = self.app.stats
            text = head
            for n, r in (("Сегодня", s.today()), ("7 дней", s.period(604800)), ("30 дней", s.period(2592000))):
                x = r[platform]
                text += f"<b>{n}</b>\n・ 🛒 {x['count']} {plural(x['count'], 'заказ', 'заказа', 'заказов')}\n"
                if fin:
                    text += f"・ 💵 Выручка: {rub(x['sum'])}\n・ 💰 Прибыль: <b>{rub(x['profit'])}</b>\n"
                    if x.get("no_cost"):
                        text += f"・ ⚠️ Без себестоимости: {x['no_cost']}\n"
                text += "\n"
            agg = {}
            for o in s.data["orders"]:
                if o.get("p") == platform and not o.get("refunded"):
                    a = agg.setdefault(o.get("title", "?"), [0, 0.0])
                    a[0] += 1
                    a[1] += s.profit_of(o)
            top = sorted(agg.items(), key=lambda kv: -kv[1][1])[:5]
            if top:
                text += "<b>🏆 Самое прибыльное</b>\n" + "\n".join(
                    f"・ {esc(t[:36])} — {v[0]} шт" + (f" · {rub(round(v[1], 2))}" if fin else "") for t, v in top)
            if fin:
                text += f"\n\n<i>Комиссия площадки в расчётах: {float(c.get('finance.commission.' + platform) or 0):g}%</i>"
            rows = [[("💰 Общие финансы", "duo:fin", "finance"), ("👥 Покупатели", "duo:cust", "customers")],
                    [("% Комиссия", f"duo:e:finance.commission.{platform}", "finance")], back]
            return text, rows

        if sec == "chats":
            seen, items = set(), []
            for e in reversed(list(self.app.events)):
                if e.get("kind") != "message" or e.get("platform") != platform or e.get("system"):
                    continue
                key = (e.get("account"), e.get("chat_id"))
                if key in seen:
                    continue
                seen.add(key)
                items.append(e)
                if len(items) >= 8:
                    break
            text = head
            rows = []
            if not items:
                text += "Новых сообщений с момента запуска бота пока нет.\nВсе чаты с историей — в 📱 приложении."
            for e in items:
                who = e.get("chat_name") or e.get("author") or "?"
                arrow = "↗️ вы" if e.get("by_me") else "💬"
                text += (f"<b>{esc(who)}</b> · {self._ago(e.get('time'))}\n"
                         f"{arrow}: {esc((e.get('text') or '')[:90])}\n\n")
                rows.append([(f"↩️ {who[:22]}", f"duo:reply:{e.get('account')}:{e.get('chat_id')}", "chat"),
                             ("📝", f"duo:tpl:{e.get('account')}:{e.get('chat_id')}", "chat")])
            url = self.app.public_url
            if url.startswith("https://"):
                rows.append([("📱 Все чаты в приложении", ("webapp", url))])
            rows.append(back)
            return text, rows

        if sec == "orders":
            orders = [o for o in self.app.stats.data["orders"] if o.get("p") == platform][-8:][::-1]
            text = head
            rows = []
            if not orders:
                text += "Заказов пока нет. Они появятся здесь, как только кто-то купит."
            for o in orders:
                mark = "↩️ возврат" if o.get("refunded") else ("✅ выдан" if o.get("delivered") else "⏳ ждёт")
                money = f" · {rub(o.get('price'))}" if fin else ""
                text += (f"<code>#{esc(o['id'])}</code> · {self._ago(o.get('t'))} · {mark}\n"
                         f"{esc(o.get('title', '')[:60])}\n👤 {esc(o.get('buyer') or '?')}{money}\n\n")
                if p == "fp" and not o.get("refunded"):
                    rows.append([(f"💸 Вернуть #{o['id']}", f"duo:refund:{o.get('a') or 'fp'}:{o['id']}", "refund")])
            rows = rows[:4] + [back]
            return text, rows

        if sec == "greet":
            text = head + (f"Первое сообщение новому покупателю. Не чаще раза в "
                           f"{c.get(k + '.greeting.cooldown_days')} дн. одному человеку.\n"
                           f"<b>Приветствие</b> {onoff(c.get(k + '.greeting.enabled'))}\n{q(c.get(k + '.greeting.text'))}"
                           f"<b>Постоянным покупателям</b> {onoff(c.get('features.returning_buyer.enabled'))}\n"
                           f"{q(c.get('features.returning_buyer.text'))}"
                           f"<b>Нерабочее время</b> {onoff(c.get('general.work_hours.enabled'))} "
                           f"{c.get('general.work_hours.from')}–{c.get('general.work_hours.to')}\n"
                           f"{q(c.get('general.work_hours.away_text'))}"
                           f"<b>Отпуск</b> {onoff(c.get('features.vacation.enabled'))}\n{q(c.get('features.vacation.text'))}"
                           f"<i>Переменные: $username, $orders_count, $vacation_until</i>")
            rows = [[tg(k + ".greeting.enabled", "Приветствие"), ed(k + ".greeting.text", "✏️ Текст")],
                    [ed(k + ".greeting.cooldown_days", "⏳ Раз в сколько дней")],
                    [tg("features.returning_buyer.enabled", "Постоянным"), ed("features.returning_buyer.text", "✏️ Текст")],
                    [tg("general.work_hours.enabled", "Нерабочее время"), ed("general.work_hours.range", "🕘 Часы")],
                    [ed("general.work_hours.away_text", "✏️ Текст нерабочего времени")],
                    [tg("features.vacation.enabled", "Отпуск"), ed("features.vacation.text", "✏️ Текст")],
                    [ed("features.vacation.until", "📅 Отпуск до даты")], back]
            return text, rows

        if sec == "cmds":
            cmds = [x for x in c.get("commands", []) if platform in x.get("platforms", ["funpay", "playerok"])]
            text = head + (f"Автоответ {onoff(c.get(k + '.auto_reply'))} — бот отвечает на сообщения покупателя, "
                           f"которые совпадают с командой.\n\n")
            for x in cmds:
                text += f"<code>{esc(x['trigger'])}</code>{' 🔔' if x.get('notify') else ''}\n{q(x.get('text'), 120)}"
            if not cmds:
                text += "Команд пока нет.\n"
            text += "\n<i>🔔 — заодно позвать вас в Telegram</i>"
            rows = [[tg(k + ".auto_reply", "Автоответ"), ("📋 Изменить команды", "duo:cmds", "settings")],
                    [("📝 Мои шаблоны ответов", "duo:e:templates", "settings")], back]
            return text, rows

        if sec == "dlv":
            rules = [r for r in c.get("delivery", []) if platform in r.get("platforms", ["funpay", "playerok"])]
            text = head + f"Автовыдача {onoff(c.get(k + '.auto_delivery'))} — ключевое слово в названии заказа → товар.\n\n"
            for r in rules[:10]:
                mode = r.get("mode", "file")
                cnt = Products.count(r.get("file", "")) if mode == "file" else "∞"
                src = {"file": "из файла", "supplier": "у поставщика", "text": "текст"}.get(mode, mode)
                text += f"{onoff(r.get('enabled', True))} <code>{esc(r.get('keyword'))}</code> · {src} · осталось <b>{cnt}</b>\n"
            if not rules:
                text += "Правил пока нет — нажмите «📦 Правила и товары».\n"
            text += (f"\n<b>Сообщение при заказе</b> {onoff(c.get(k + '.order_text.enabled'))}\n"
                     f"{q(c.get(k + '.order_text.text'))}")
            rows = [[tg(k + ".auto_delivery", "Автовыдача"), ("📦 Правила и товары", "duo:dlv", "products")],
                    [tg(k + ".order_text.enabled", "Сообщение при заказе"), ed(k + ".order_text.text", "✏️ Текст")]]
            rows.append([tg("funpay.auto_refund_no_stock", "Возврат, если товар кончился")] if p == "fp"
                        else [tg("playerok.mark_sent", "Отмечать сделку отправленной")])
            rows.append(back)
            return text, rows

        if sec == "raise" and p == "fp":
            info = [x for svc in pool.values() for x in svc.raise_info.values()]
            text = head + (f"Автоподнятие {onoff(c.get('funpay.auto_raise.enabled'))} · проверка раз в "
                           f"{c.get('funpay.auto_raise.check_every_min')} мин\n"
                           f"FunPay разрешает поднимать каждую категорию раз в несколько часов — бот делает это сам, "
                           f"как только можно.\n\n"
                           + ("<b>Последние поднятия</b>\n" + "\n".join(f"・ {esc(x)[:90]}" for x in info[-8:])
                              if info else "Ещё не поднимал."))
            rows = [[tg("funpay.auto_raise.enabled", "Автоподнятие"), ("⬆️ Поднять сейчас", "duo:raise", "settings")],
                    [ed("funpay.auto_raise.check_every_min", "⏱ Интервал проверки, мин")], back]
            return text, rows

        if sec == "confirm":
            text = head + (f"<b>Спасибо после подтверждения</b> {onoff(c.get(k + '.confirm_reply.enabled'))}\n"
                           f"{q(c.get(k + '.confirm_reply.text'))}"
                           f"<b>Напомнить подтвердить</b> {onoff(c.get('features.confirm_reminder.enabled'))} · через "
                           f"{c.get('features.confirm_reminder.after_hours')} ч\n{q(c.get('features.confirm_reminder.text'))}"
                           f"<i>Переменные: $username, $order_id, $order_title</i>")
            rows = [[tg(k + ".confirm_reply.enabled", "Спасибо"), ed(k + ".confirm_reply.text", "✏️ Текст")],
                    [tg("features.confirm_reminder.enabled", "Напоминание"), ed("features.confirm_reminder.text", "✏️ Текст")],
                    [ed("features.confirm_reminder.after_hours", "⏰ Через сколько часов")], back]
            return text, rows

        if sec == "reviews" and p == "fp":
            texts = c.get("funpay.review_reply.texts", {})
            text = head + f"Ответ на отзыв {onoff(c.get('funpay.review_reply.enabled'))} — свой текст на каждую оценку.\n\n"
            for star in ("5", "4", "3", "2", "1"):
                text += f"{'⭐' * int(star)}\n{q(texts.get(star), 120)}"
            rows = [[tg("funpay.review_reply.enabled", "Отвечать на отзывы")],
                    [ed(f"funpay.review_reply.texts.{st}", f"✏️ {st}⭐") for st in ("5", "4", "3")],
                    [ed(f"funpay.review_reply.texts.{st}", f"✏️ {st}⭐") for st in ("2", "1")], back]
            return text, rows

        if sec == "accs":
            lines = []
            for a in c.accounts(platform):
                svc = pool.get(a["id"])
                stt = "⚪️ выкл" if not a.get("enabled", True) else (onoff(svc and svc.ok) if svc else "🟡 нет ключа")
                lines.append(f"{stt} <b>{esc(self.app.account_name(a['id']))}</b>"
                             + (f" — {esc(svc.error)}" if svc and svc.error and not svc.ok else "")
                             + f"\n    🌐 {esc(proxy_label(a.get('proxy', '')))}")
            text = head + ("\n".join(lines) or "Аккаунтов пока нет.") + \
                "\n\n<i>Нажмите на аккаунт: вкл/выкл, прокси, ключ, автовыдача. Применяется сразу.</i>"
            rows = [[(f"👤 {self.app.account_name(a['id'])[:28]}", f"duo:acc:{a['id']}")] for a in c.accounts(platform)]
            rows += [[("➕ Добавить аккаунт", f"duo:addacc:{p}", "secrets")], back]
            return text, rows

        if sec == "wd":
            w = c.get(f"withdraw.{platform}", {})
            mode = w.get("mode", "off")
            paused = self.app.withdraw.paused(platform)
            names = {"off": "выключен", "ask": "спрашивать меня", "auto": "автоматически"}
            meth = w.get("method") or w.get("provider")
            text = head + (f"Режим: <b>{names.get(mode, mode)}</b>\n"
                           f"・ 💳 Куда: {esc(METHOD_NAMES.get(meth, meth or '—'))} "
                           f"{mask_wallet(w.get('wallet')) if w.get('wallet') else '<i>(реквизиты не указаны)</i>'}\n"
                           f"・ 💰 Выводить, когда баланс ≥ {rub(w.get('threshold', 0))}\n"
                           f"・ 🏦 Оставлять на балансе {rub(w.get('keep', 0))}\n"
                           f"・ ⬇️ Мин. сумма {rub(w.get('min_amount', 0))} · 📅 до {rub(w.get('max_per_day', 0))} в день"
                           + (f"\n\n⏸ <b>На паузе:</b> {esc(paused)}" if paused else "")
                           + "\n\n<i>Реквизиты указываются в 📱 приложении — так безопаснее.</i>")
            nxt = {"off": "ask", "ask": "auto", "auto": "off"}[mode if mode in names else "off"]
            rows = [[(f"🔁 Режим: {names.get(mode, mode)}", f"duo:wdm:{p}:{nxt}", "withdraw"),
                     ("💸 Вывести сейчас", f"duo:wdnow:{platform}", "withdraw")],
                    [("💰 Порог", f"duo:e:withdraw.{platform}.threshold", "withdraw"),
                     ("🏦 Оставлять", f"duo:e:withdraw.{platform}.keep", "withdraw")],
                    [("⬇️ Мин. сумма", f"duo:e:withdraw.{platform}.min_amount", "withdraw"),
                     ("📅 Лимит в день", f"duo:e:withdraw.{platform}.max_per_day", "withdraw")]]
            if paused:
                rows.append([("▶️ Снять паузу", f"duo:wdun:{platform}", "withdraw")])
            rows.append(back)
            return text, rows

        if sec == "lots":
            o = c.get("features.out_of_stock", {})
            names = {"off": "ничего не делать", "notify": "только сообщить", "ask": "спросить меня", "auto": "скрывать сразу"}
            rn = {"off": "ничего", "ask": "спросить меня", "auto": "показывать сразу"}
            text = head + (f"Когда в файле автовыдачи кончается товар, лот можно скрыть, чтобы не продавать пустое.\n\n"
                           f"・ 📭 Товар кончился: <b>{names.get(o.get('mode'), o.get('mode'))}</b>\n"
                           f"・ 📬 Товар снова есть: <b>{rn.get(o.get('restore'), o.get('restore'))}</b>\n"
                           f"・ 📉 Сообщить, когда осталось ≤ {c.get('features.low_stock.threshold')} шт: "
                           f"{onoff(c.get('features.low_stock.enabled'))}\n\n"
                           f"<i>Общее для обеих площадок. Какие лоты к какому товару — в 📱 приложении.</i>")
            m1 = {"off": "notify", "notify": "ask", "ask": "auto", "auto": "off"}.get(o.get("mode"), "ask")
            m2 = {"off": "ask", "ask": "auto", "auto": "off"}.get(o.get("restore"), "ask")
            rows = [[(f"🔁 Кончился: {names.get(o.get('mode'), '?')}", f"duo:oos:{p}:mode:{m1}", "delivery")],
                    [(f"🔁 Снова есть: {rn.get(o.get('restore'), '?')}", f"duo:oos:{p}:restore:{m2}", "delivery")],
                    [tg("features.low_stock.enabled", "Мало товара"), ed("features.low_stock.threshold", "📉 Порог")],
                    back]
            return text, rows

        if sec == "ntf":
            keys = [x for x in NOTIFY_NAMES if x.startswith(p + "_")]
            text = head + "О чём присылать сообщения по этой площадке:"
            nm = lambda x: NOTIFY_NAMES[x].split(" ", 1)[1].capitalize()  # noqa: E731
            rows = [[tg(f"telegram.notify.{x}", nm(x)) for x in keys[i:i + 2]] for i in range(0, len(keys), 2)]
            rows += [[("🔔 Все уведомления бота", "duo:ntf", "settings")], back]
            return text, rows
        return head + "Раздел не найден", [back]

    def account_card(self, acc_id):
        a = self.cfg.account(acc_id)
        if not a:
            return "Аккаунт не найден", [[("◀️ Назад", "duo:main")]]
        platform = "funpay" if acc_id.startswith("fp") else "playerok"
        p = "fp" if platform == "funpay" else "pl"
        svc = (self.app.fp if platform == "funpay" else self.app.pl).get(acc_id)
        uname = (svc.client.username if platform == "funpay" else svc.bridge.username) if svc else None
        key = "golden_key" if platform == "funpay" else "token"
        text = (f"<b>👤 {esc(self.app.account_name(acc_id))}</b> · <code>{acc_id}</code>\n"
                f"Площадка: {'FunPay' if platform == 'funpay' else 'Playerok'}\n"
                f"Логин на сайте: {esc(uname or '—')}\nСостояние: "
                f"{'работает' if svc and svc.ok else (esc(svc.error) if svc else 'не запущен')}\n"
                f"Ключ: {'сохранён' if a.get(key) or a.get('cookies') else 'нет'} · прокси: {esc(proxy_label(a.get('proxy', '')))}")
        rows = [[(f"{onoff(a.get('enabled', True))} Включён", f"duo:acct:{acc_id}:enabled"),
                 (f"{onoff(a.get('auto_delivery', True))} Автовыдача", f"duo:acct:{acc_id}:auto_delivery")]]
        if platform == "funpay":
            rows.append([(f"{onoff(a.get('auto_raise', True))} Автоподнятие", f"duo:acct:{acc_id}:auto_raise")])
        rows += [[("✏️ Название", f"duo:e:acc.{acc_id}.name"), ("🌐 Прокси", f"duo:e:acc.{acc_id}.proxy")],
                 [(f"🔑 Сменить {key}", f"duo:e:acc.{acc_id}.{key}", "secrets")],
                 [("🗑 Удалить аккаунт", f"duo:accdel:{acc_id}", "secrets"), ("◀️ Аккаунты", "duo:accs")]]
        return text + "\n\n<i>Изменения применяются сразу, без перезапуска.</i>", rows

    # ---------- финансы ----------
    def finance_menu(self, uid):
        s = self.app.stats
        names = (("Сегодня", s.today()), ("7 дней", s.period(604800)), ("30 дней", s.period(2592000)))
        text = "<b>💰 Финансы</b>\n"
        for n, r in names:
            nc = r["funpay"]["no_cost"] + r["playerok"]["no_cost"]
            text += (f"\n<b>{n}</b>: выручка {r['total']['sum']} · прибыль <b>{r['total']['profit']}</b>\n"
                     f"  🔵 {r['funpay']['count']} зак. · {r['funpay']['sum']} → {r['funpay']['profit']}\n"
                     f"  🟢 {r['playerok']['count']} зак. · {r['playerok']['sum']} → {r['playerok']['profit']}"
                     + (f"\n  расходы: {r['total']['expenses']}" if r["total"]["expenses"] else "")
                     + (f"\n  ⚠️ без себестоимости: {nc}" if nc else ""))
        top = s.top_products(5)
        if top:
            text += "\n\n<b>Самое прибыльное (всё время)</b>\n" + "\n".join(
                f"• {esc(x['title'][:40])} — {x['count']} шт · {x['profit']}" for x in top)
        rows = []
        if "withdraw" in self.perms(uid):
            for p in ("funpay", "playerok"):
                w = self.cfg.get(f"withdraw.{p}", {})
                paused = self.app.withdraw.paused(p)
                text += (f"\n\n💸 Автовывод {'FunPay' if p == 'funpay' else 'Playerok'}: "
                         f"{ {'off': 'выкл', 'ask': 'спрашивать', 'auto': 'авто'}.get(w.get('mode'), w.get('mode')) }"
                         f" · {esc(w.get('method') or w.get('provider') or '—')} {mask_wallet(w.get('wallet'))}"
                         + (f"\n⏸ На паузе: {esc(paused)}" if paused else ""))
                row = [(f"💸 Вывести с {'FunPay' if p == 'funpay' else 'Playerok'}", f"duo:wdnow:{p}", "withdraw")]
                if paused:
                    row.append(("▶️ Снять паузу", f"duo:wdun:{p}", "withdraw"))
                rows.append(row)
        text += "\n\n<i>Себестоимость, комиссии, расходы и реквизиты — в мини-аппе.</i>"
        rows.append([("◀️ Назад", "duo:main")])
        return text, rows

    # ---------- команда ----------
    def team_menu(self):
        users = self.app.team.users()
        text = "<b>👨‍💼 Команда</b>\n\n" + "\n".join(
            f"• <code>{u['id']}</code> {esc(u.get('name') or '')} — {ROLE_NAMES.get(u.get('role'), u.get('role'))}"
            for u in users) + "\n\n" + "\n".join(f"<b>{ROLE_NAMES[r]}</b>: {ROLE_HINTS[r]}" for r in ROLES)
        rows = [[(f"{ROLE_NAMES.get(u.get('role'), '')[:1]} {u.get('name') or u['id']}", f"duo:usr:{u['id']}")] for u in users]
        rows += [[("➕ Пригласить менеджера", "duo:inv:manager"), ("➕ Админа", "duo:inv:admin")],
                 [("➕ Совладельца", "duo:inv:owner")], [("◀️ Назад", "duo:main")]]
        return text, rows

    def user_card(self, uid):
        role = self.app.team.role(uid)
        u = next((x for x in self.app.team.users() if int(x["id"]) == int(uid)), {})
        text = f"<b>👤 {esc(u.get('name') or uid)}</b> <code>{uid}</code>\nРоль: {ROLE_NAMES.get(role, role)}"
        rows = [[(("✅ " if r == role else "") + ROLE_NAMES[r], f"duo:urole:{uid}:{r}") for r in ROLES],
                [("🗑 Убрать из команды", f"duo:udel:{uid}"), ("◀️ Назад", "duo:team")]]
        return text, rows

    def general_menu(self):
        c = self.cfg
        text = (f"<b>⚙️ Общее</b>\nРабочие часы: {onoff(c.get('general.work_hours.enabled'))} "
                f"{c.get('general.work_hours.from')}–{c.get('general.work_hours.to')}\n"
                f"Подпись в чатах: {esc(watermark(c) or 'выключена')}\n"
                f"ЧС: {', '.join(esc(u) for u in c.get('blacklist.users', [])) or '—'}\n"
                f"Веб-панель: {esc(self.app.public_url or 'http://IP:' + str(c.get('web.port')))}")
        rows = [[(f"{onoff(c.get('general.work_hours.enabled'))} Рабочие часы", "duo:t:general.work_hours.enabled:gen"),
                 ("🕘 Часы", "duo:e:general.work_hours.range")],
                [("✏️ Текст нерабочего времени", "duo:e:general.work_hours.away_text")],
                [("✍️ Подпись в чатах", "duo:wm"), ("📝 Шаблоны", "duo:e:templates")],
                [("🚫 ЧС +", "duo:e:blacklist.add"), ("✅ ЧС −", "duo:e:blacklist.remove")],
                [("🔐 Пароль панели", "duo:e:web.password", "secrets"), ("🔗 URL мини-аппа", "duo:e:web.public_url", "system")],
                [("📣 Чаты для уведомлений", "duo:e:extra_chats", "system")],
                [("◀️ Назад", "duo:main")]]
        return text, rows

    def stats_text(self):
        return self.finance_menu(0)[0]

    def status_text(self, uid=None):
        st = self.app.status()
        fin = uid is None or "finance" in self.perms(uid)
        lines = []
        for a in st["accounts"]:
            bal = f" · {a['balance']} {esc(a['currency'])}" if fin and a["balance"] is not None else ""
            stt = "⚪️" if not a["running"] else onoff(a["ok"])
            lines.append(f"{'🔵' if a['platform'] == 'funpay' else '🟢'} {stt} {esc(a['name'])}{bal}"
                         + (f"\n    {esc(a['error'])}" if a["error"] and not a["ok"] else ""))
        prods = "\n".join(f"  • {esc(k)}: {v}" for k, v in st["products"].items()) or "  —"
        tg = st["telegram"]
        return (f"<b>📊 Статус</b> · v{VERSION} · ⏱ {st['uptime']}\n\n" + ("\n".join(lines) or "Аккаунтов нет")
                + f"\n\n🤖 Telegram: @{esc(tg['username'])} · прокси: {esc(tg['proxy'] or 'нет')}\n"
                f"🌐 Панель: {esc(st['web']['url'] or st['web']['local'])}\n\n📦 Товары:\n{prods}")

    def features_menu(self):
        c = self.cfg
        f = lambda k: c.get(f"features.{k}.enabled")  # noqa: E731
        text = ("<b>✨ Функции</b>\n\n"
                f"⭐ Постоянные покупатели — отдельное приветствие при повторном заказе\n"
                f"⏰ Напоминание подтвердить заказ через {c.get('features.confirm_reminder.after_hours')} ч\n"
                f"🌙 Отчёт за день в {c.get('features.daily_report.time')}\n"
                f"📉 Уведомление, когда товара ≤ {c.get('features.low_stock.threshold')}\n"
                f"🛡 Антифлуд: не больше {c.get('features.anti_flood.max_replies_per_min')} автоответов в минуту на чат\n"
                f"💾 Автобекап раз в сутки (хранить {c.get('features.auto_backup.keep')})\n"
                f"🔗 Вебхуки (Discord и др.): {len(c.get('features.webhooks.urls', []))} адр.\n"
                f"💸 Автовозврат FunPay, если товар закончился")
        rows = [[(f"{onoff(f('returning_buyer'))} Постоянные", "duo:t:features.returning_buyer.enabled:feat"),
                 (f"{onoff(f('confirm_reminder'))} Напоминания", "duo:t:features.confirm_reminder.enabled:feat")],
                [(f"{onoff(f('daily_report'))} Отчёт", "duo:t:features.daily_report.enabled:feat"),
                 (f"{onoff(f('low_stock'))} Остатки", "duo:t:features.low_stock.enabled:feat")],
                [(f"{onoff(f('anti_flood'))} Антифлуд", "duo:t:features.anti_flood.enabled:feat"),
                 (f"{onoff(f('auto_backup'))} Автобекап", "duo:t:features.auto_backup.enabled:feat")],
                [(f"{onoff(f('webhooks'))} Вебхуки", "duo:t:features.webhooks.enabled:feat"),
                 (f"{onoff(c.get('funpay.auto_refund_no_stock'))} Автовозврат", "duo:t:funpay.auto_refund_no_stock:feat")],
                [("⏰ Часы до напоминания", "duo:e:features.confirm_reminder.after_hours"),
                 ("🌙 Время отчёта", "duo:e:features.daily_report.time")],
                [("📉 Порог остатков", "duo:e:features.low_stock.threshold"), ("🔗 Вебхуки", "duo:e:webhooks")],
                [("🏖 Отпуск: до даты", "duo:e:features.vacation.until"), ("✏️ Текст отпуска", "duo:e:features.vacation.text")],
                [("◀️ Назад", "duo:main")]]
        return text, rows

    def customers_menu(self):
        top = self.app.customers.list()[:15]
        text = "<b>👥 Лучшие покупатели</b>\n\n" + ("\n".join(
            f"{'🔵' if c['platform'] == 'funpay' else '🟢'} <b>{esc(c['name'])}</b> — {c['orders']} зак. · {c['spent']}"
            + (f"\n   📝 {esc(c['note'])}" if c.get("note") else "") for c in top) or "Пока нет покупателей")
        return text + "\n\nЗаметку о покупателе можно добавить в мини-аппе.", [[("◀️ Назад", "duo:main")]]

    def cmds_menu(self, back="main"):
        cmds = self.cfg.get("commands", [])
        text = "<b>💬 Автоответы</b>\nНажмите на команду, чтобы удалить её.\n\n"
        rows = []
        for i, c in enumerate(cmds):
            pl = "".join("🔵" if x == "funpay" else "🟢" for x in c.get("platforms", []))
            text += f"{i + 1}. <code>{esc(c['trigger'])}</code> {pl}{' 🔔' if c.get('notify') else ''} → {esc(c.get('text', ''))[:60]}\n"
            rows.append([(f"🗑 {c['trigger'][:30]}", f"duo:cdel:{i}"), (f"🔔 {onoff(c.get('notify'))}", f"duo:cntf:{i}")])
        rows.append([("➕ Добавить", "duo:cadd")])
        rows.append([("◀️ Назад", f"duo:{back}")])
        return text, rows

    def dlv_menu(self, back="main"):
        rules = self.cfg.get("delivery", [])
        text = "<b>📦 Автовыдача</b>\nПравило: ключевое слово в названии заказа → товар из файла.\n\n"
        rows = []
        for i, r in enumerate(rules):
            mode = r.get("mode", "file")
            cnt = Products.count(r.get("file", "")) if mode == "file" else "∞"
            src = r.get("file") if mode == "file" else (f"поставщик {r.get('supplier')}" if mode == "supplier" else "текст")
            text += (f"{i + 1}. {onoff(r.get('enabled', True))} <code>{esc(r.get('keyword'))}</code> → "
                     f"{esc(src)} (осталось: {cnt})\n")
            rows.append([(f"{onoff(r.get('enabled', True))} {r.get('keyword', '')[:20]}", f"duo:dtog:{i}", "delivery"),
                         ("➕ Товары", f"duo:dadd:{i}", "products"), ("🗑", f"duo:ddel:{i}", "delivery")])
        rows.append([("➕ Новое правило", "duo:dnew", "delivery")])
        rows.append([("◀️ Назад", f"duo:{back}")])
        return text, rows

    def notify_menu(self):  # noqa: D401
        rows, row = [], []
        for k, name in NOTIFY_NAMES.items():
            row.append((f"{onoff(self.cfg.get('telegram.notify.' + k))} {name}", f"duo:t:telegram.notify.{k}:ntf"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("◀️ Назад", "duo:main")])
        return "<b>🔔 Уведомления</b>", rows

    def plugins_menu(self, offset=0):
        pls = list(self.app.plugins.plugins.values())
        text = f"<b>🧩 Плагины</b> ({len(pls)})\nОтправьте .py файл в этот чат, чтобы установить плагин.\n"
        if self.app.plugins.failed:
            text += "\n⚠️ Не загрузились: " + ", ".join(esc(f["file"]) for f in self.app.plugins.failed)
        rows = []
        for i in pls[offset:offset + 8]:
            rows.append([(f"{onoff(self.app.plugins.enabled(i.uuid))} {i.name[:28]}", f"duo:pi:{i.uuid}")])
        nav = []
        if offset > 0:
            nav.append(("⬅️", f"duo:plg:{max(0, offset - 8)}"))
        if offset + 8 < len(pls):
            nav.append(("➡️", f"duo:plg:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("◀️ Назад", "duo:main")])
        return text, rows

    def plugin_card(self, uid):
        i = self.app.plugins.plugins.get(uid)
        if not i:
            return "Плагин не найден", [[("◀️ Назад", "duo:plg:0")]]
        en = self.app.plugins.enabled(uid)
        text = (f"<b>🧩 {esc(i.name)}</b> v{esc(i.version)}\n{esc(i.description)}\n\n"
                f"Автор: {esc(i.credits)}\nТип: {i.kind}\nФайл: <code>{esc(i.path.name)}</code>\n"
                f"UUID: <code>{i.uuid}</code>\nХуки: {', '.join(h.replace('BIND_TO_', '') for h in i.hooks) or '—'}")
        if i.commands:
            text += "\nКоманды: " + ", ".join("/" + c["cmd"] for c in i.commands)
        rows = [[(f"{onoff(en)} {'Выключить' if en else 'Включить'}", f"duo:ptog:{uid}")]]
        if i.settings_page:
            rows.append([("⚙️ Настройки", f"{CBT.PLUGIN_SETTINGS}:{uid}:0")])
        rows.append([("🗑 Удалить", f"duo:pdel:{uid}"), ("◀️ Назад", "duo:plg:0")])
        return text, rows

    def _kind_rows(self, i, p):
        cur = proxy_kind(p)
        return [[(("• " if cur == k else "") + ("Авто" if k == "auto" else t), f"duo:xkind:{i}:{k}")
                 for k, t in PROXY_KINDS.items()]]

    def _check_acc_proxy(self, cid, platform, acc_id, p):
        """Проверяет прокси аккаунта на сайте площадки и запоминает подошедший тип подключения."""
        from .core import diagnose_proxy
        d = diagnose_proxy(p, "https://funpay.com" if platform == "funpay" else "https://playerok.com")
        if d["ok"]:
            fixed = canonical_proxy(d["proxy"], proxy_kind(p))
            if fixed != p:
                accs = self.cfg.accounts(platform)
                for a in accs:
                    if a["id"] == acc_id and a.get("proxy") == p:
                        a["proxy"] = fixed
                self.cfg.set(f"accounts.{platform}", accs)
                self.app.sync_accounts()
            self.send(cid, f"✅ Прокси аккаунта работает: {d['ms']} мс (подключение {scheme_title(d['proxy'])})")
        else:
            self.send(cid, f"⚠️ Прокси аккаунта не открыл сайт площадки: {esc(d['reason'])}",
                      [[("👤 Аккаунт", f"duo:acc:{acc_id}")]])

    def _test_and_report(self, cid, p):
        from .core import diagnose_proxy
        d = diagnose_proxy(p)
        if d["ok"]:
            fixed = canonical_proxy(d["proxy"], proxy_kind(p))
            if fixed != p:  # запоминаем подошедший тип подключения (например, SOCKS5 вместо HTTP)
                lst = self.cfg.get("telegram.proxy.list", [])
                lst = [fixed if x == p else x for x in lst]
                self.cfg.set("telegram.proxy.list", lst)
                p = fixed
            msg = f"✅ <code>{esc(proxy_label(p))}</code> работает: {d['ms']} мс (подключение {scheme_title(d['proxy'])})"
            self.app.proxy.results[p] = {"ok": True, "info": d["ms"], "t": time.time(), "variant": d["proxy"]}
        else:
            msg = f"❌ <code>{esc(proxy_label(p))}</code>: {esc(d['reason'])}"
            if d.get("kind") == "ipv6" and proxy_kind(p) == "auto":
                msg += "\n<i>Проверка показала, что это IPv6-прокси.</i>"
        self.send(cid, msg, [[("🤖 Бот и прокси", "duo:prx")]])

    def proxy_menu(self):
        pc = self.cfg.get("telegram.proxy", {})
        pm = self.app.proxy
        text = (f"<b>🤖 Бот и прокси</b>\nБот: @{esc(self.username)} {onoff(self.ok)}\n"
                f"Прокси: {onoff(pc.get('enabled'))} · сейчас <code>{esc(pm.label())}</code>\n"
                f"Автопереключение: {onoff(pc.get('failover', True))}\n\nСписок (берётся первый рабочий):\n")
        rows = []
        for i, p in enumerate(pc.get("list", [])):
            r = pm.results.get(p)
            mark = "✅" if r and r["ok"] else ("❌" if r else "▫️")
            cur = " ← сейчас" if p == pm.current else ""
            text += f"{i + 1}. {mark} <code>{esc(proxy_label(p))}</code>{cur}\n"
            rows.append([(f"▶️ {proxy_label(p)[:22]}", f"duo:xuse:{i}"), ("🧪", f"duo:xtest:{i}"),
                         ("🏷", f"duo:xkinds:{i}"), ("🗑", f"duo:xdel:{i}")])
        rows.append([(f"{onoff(pc.get('enabled'))} Прокси", "duo:xtoggle"),
                     (f"{onoff(pc.get('failover', True))} Автопереключение", "duo:t:telegram.proxy.failover:prx")])
        rows.append([("➕ Добавить прокси", "duo:xadd"), ("🧪 Проверить все", "duo:xall")])
        rows.append([("🔁 Применить сейчас", "duo:xapply"), ("🤖 Сменить бота", "duo:e:telegram.token", "secrets")])
        rows.append([("◀️ Назад", "duo:main")])
        text += ("\nПрокси можно присылать в любом виде — бот сам приведёт запись к нужному формату. "
                 "🏷 — тип: IPv4, IPv4 Shared или IPv6.\n<i>Изменения применяются сразу, без перезапуска.</i>")
        return text, rows

    # ---------- регистрация хендлеров ----------
    def _register(self):
        bot = self.bot

        @bot.message_handler(commands=["start", "menu", "login"])
        def _start(m):
            uid = m.from_user.id
            arg = ((m.text or "").split(maxsplit=1) + [""])[1].strip()
            name = (m.from_user.username and "@" + m.from_user.username) or getattr(m.from_user, "first_name", "") or ""
            team = self.app.team
            if arg.startswith("inv_"):
                role = team.redeem(arg[4:], uid, name)
                if role:
                    log.info("TG: %s (%s) вошёл в команду как %s", uid, name, role)
                    bot.send_message(uid, f"✅ Вы в команде: <b>{ROLE_NAMES[role]}</b>\n{ROLE_HINTS[role]}")
                    self.notify(f"👨‍💼 {esc(name)} (<code>{uid}</code>) присоединился как {ROLE_NAMES[role]}", perm="users")
                else:
                    bot.send_message(uid, "❌ Приглашение недействительно или устарело. Попросите новое.")
                    return
            elif not team.role(uid):
                pwd = self.cfg.get("telegram.password")
                if not team.users() and m.text.startswith("/login") and pwd and hmac.compare_digest(arg, pwd):
                    team.add(uid, "owner", name)
                    bot.reply_to(m, "✅ Вы — владелец бота. Помощников добавляйте через «👨‍💼 Команда».")
                    log.info("TG: владелец %s (%s)", uid, name)
                elif team.users():
                    lk = links_html(self.cfg, esc)
                    bot.reply_to(m, f"{SIGN} Привет, <b>{esc(getattr(m.from_user, 'first_name', '') or '')}</b>!\n\n"
                                    f"❌ Это приватный бот <b>{APP_NAME}</b>.\nВаш ID: <code>{uid}</code>\n"
                                    f"Чтобы получить доступ, попросите владельца прислать приглашение."
                                    + (f"\n\n🛠 Хотите такого же бота себе?\n{lk}" if lk else ""),
                                 **NOPREVIEW)
                    return
                else:
                    bot.reply_to(m, f"{SIGN} Привет! Это <b>{APP_NAME}</b>.\n\n🔑 У бота ещё нет владельца.\n"
                                    f"Ваш ID: <code>{uid}</code>\nВойдите: <code>/login ПАРОЛЬ</code> "
                                    f"(пароль из мастера настройки или config.json)")
                    return
            t, r = self.main_menu(uid)
            self.send(m.chat.id, t, r, uid)

        member = lambda m: self.is_admin(m.from_user.id)  # noqa: E731

        def need(perm):
            return lambda m: self.can(m.from_user.id, perm)

        @bot.message_handler(commands=["status"], func=need("orders"))
        def _st(m):
            self.send(m.chat.id, self.status_text(m.from_user.id), [[("◀️ Меню", "duo:main")]], m.from_user.id)

        @bot.message_handler(commands=["stats", "finance"], func=need("finance"))
        def _stats(m):
            t, r = self.finance_menu(m.from_user.id)
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["raise"], func=need("settings"))
        def _raise(m):
            self._do_raise(m.chat.id)

        @bot.message_handler(commands=["plugins"], func=need("plugins"))
        def _plg(m):
            t, r = self.plugins_menu()
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["logs"], func=need("system"))
        def _logs(m):
            self._send_logs(m.chat.id)

        @bot.message_handler(commands=["restart"], func=need("system"))
        def _rs(m):
            bot.send_message(m.chat.id, "🔄 Перезапускаюсь…")
            threading.Thread(target=self.app.restart, daemon=True).start()

        @bot.message_handler(commands=["app", "panel"], func=member)
        def _app(m):
            t, r = self.webapp_menu()
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["help"], func=member)
        def _help(m):
            t, r = self.help_menu()
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["settings"], func=member)
        def _settings(m):
            t, r = self.settings_menu(m.from_user.id)
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["proxy", "bot"], func=need("system"))
        def _proxy(m):
            t, r = self.proxy_menu()
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["team"], func=need("users"))
        def _team(m):
            t, r = self.team_menu()
            self.send(m.chat.id, t, r, m.from_user.id)

        @bot.message_handler(commands=["vacation"], func=need("settings"))
        def _vac(m):
            v = self.cfg.toggle("features.vacation.enabled")
            bot.reply_to(m, "🏖 Режим отпуска включён" if v else "Режим отпуска выключен")

        @bot.message_handler(content_types=["document"],
                             func=lambda m: member(m) and not self._in_foreign_state(m))
        def _doc(m):
            self._on_document(m)

        @bot.message_handler(content_types=["text"],
                             func=lambda m: member(m) and self._own_state(m) is not None)
        def _text_state(m):
            self._on_state_text(m)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("duo:") and self.is_admin(c.from_user.id))
        def _cb(c):
            try:
                action = c.data.split(":")[1]
                perm = CB_PERM.get(action, "settings")
                if perm and not self.can(c.from_user.id, perm):
                    bot.answer_callback_query(c.id, "⛔ Недостаточно прав для этого действия", show_alert=True)
                    return
                self._on_callback(c)
            except Exception as e:
                log.error("TG callback %s: %s", c.data, e)
                log.debug(traceback.format_exc())
                try:
                    bot.answer_callback_query(c.id, f"Ошибка: {e}"[:190], show_alert=True)
                except Exception:
                    pass

        @bot.callback_query_handler(func=lambda c: c.data == CBT.CLEAR_STATE and self.is_admin(c.from_user.id))
        def _clear(c):
            self.clear_state(c.message.chat.id, c.from_user.id, True)
            bot.answer_callback_query(c.id, "Отменено")

    def register_fallbacks(self):
        """После плагинов: кнопки «назад» Cardinal → главное меню."""
        bot = self.bot

        @bot.callback_query_handler(func=lambda c: self.is_admin(c.from_user.id) and (c.data in (
            CBT.MAIN, CBT.MAIN2, CBT.MAIN3, CBT.PLUGINS_LIST, CBT.BACK) or str(c.data).startswith(
            (CBT.EDIT_PLUGIN + ":", CBT.PLUGINS_LIST + ":"))))
        def _fallback(c):
            if str(c.data).startswith(CBT.EDIT_PLUGIN + ":") and self.can(c.from_user.id, "plugins"):
                t, r = self.plugin_card(c.data.split(":")[1])
            elif str(c.data).startswith(CBT.PLUGINS_LIST) and self.can(c.from_user.id, "plugins"):
                t, r = self.plugins_menu()
            else:
                t, r = self.main_menu(c.from_user.id)
            self.edit(c, t, r)
            bot.answer_callback_query(c.id)

    # ---------- помощники ----------
    def _own_state(self, m):
        s = self.get_state(m.chat.id, m.from_user.id)
        return s if s and str(s["state"]).startswith("duo_") else None

    def _in_foreign_state(self, m):
        s = self.get_state(m.chat.id, m.from_user.id)
        return bool(s and not str(s["state"]).startswith("duo_"))

    def ask(self, chat_id, user_id, state, prompt, data=None):
        msg = self.bot.send_message(chat_id, prompt + "\n\n<i>/cancel — отмена</i>",
                                    reply_markup=kb([[("❌ Отмена", "duo:cancel")]]))
        self.set_state(chat_id, msg.message_id, user_id, state, data)

    @staticmethod
    def _chat(v):
        return int(v) if str(v).lstrip("-").isdigit() else v

    def _resolve_acc(self, acc):
        """Старые кнопки «fp»/«pl» → основной аккаунт площадки."""
        if acc in ("fp", "pl"):
            svc = self.app.service("funpay" if acc == "fp" else "playerok")
            return svc.acc_id if svc else acc
        return acc

    @staticmethod
    def _platform(acc):
        return "funpay" if str(acc).startswith("fp") else "playerok"

    def _do_raise(self, chat_id):
        if not any(s.ok for s in self.app.fp.values()):
            self.bot.send_message(chat_id, "FunPay не подключён")
            return
        self.bot.send_message(chat_id, "⬆️ Поднимаю лоты…")

        def run():
            rep = []
            for svc in list(self.app.fp.values()):
                if svc.ok:
                    try:
                        rep += [f"[{self.app.account_name(svc.acc_id)}] {x}" for x in svc.raise_all(force=True)]
                    except Exception as e:
                        rep.append(f"[{svc.acc_id}] ошибка: {e}")
            self.bot.send_message(chat_id, "\n".join(esc(x) for x in rep) or "Лотов не найдено")
        threading.Thread(target=run, daemon=True).start()

    def _apply_and_report(self, chat_id):
        ok, info = self.app.reload_telegram()
        tg = self.app.tg
        if tg and tg.bot:
            try:
                tg.bot.send_message(chat_id, f"{'✅' if ok else '❌'} Telegram: {esc(info)}",
                                    reply_markup=kb([[("🤖 Бот и прокси", "duo:prx")]]))
            except Exception:
                pass

    def _send_logs(self, chat_id):
        p = LOGS_DIR / "fpcastor.log"
        if p.exists():
            with open(p, "rb") as f:
                self.bot.send_document(chat_id, f, caption="📜 Лог", visible_file_name="fpcastor.log")
        else:
            self.bot.send_message(chat_id, "Лог пуст")

    def _find_order(self, oid):
        o = next((x for x in reversed(self.app.events) if x.get("kind") == "order" and x.get("id") == str(oid)), None)
        if o:
            return o
        s = self.app.stats.find(oid)
        if s:
            cust = self.app.customers.get(s["p"], s["buyer"]) or {}
            return {"platform": s["p"], "account": s.get("a"), "id": s["id"], "title": s["title"], "price": s["price"],
                    "buyer": s["buyer"], "chat_id": cust.get("chat_id"), "amount": s.get("amount", 1)}
        return None

    def _on_document(self, m):
        s = self._own_state(m)
        data = self.bot.download_file(self.bot.get_file(m.document.file_id).file_path)
        name = m.document.file_name or "file"
        if s and s["state"] == "duo_dadd":
            lines = data.decode("utf-8", "ignore").splitlines()
            rule = self.cfg.get("delivery")[s["data"]["i"]]
            n = Products.add(rule["file"], lines)
            self.clear_state(m.chat.id, m.from_user.id, True)
            self.bot.reply_to(m, f"✅ Добавлено. В файле <code>{esc(rule['file'])}</code> теперь {n} шт.")
            return
        uid = m.from_user.id
        if name.endswith(".py"):
            if not self.can(uid, "plugins"):
                self.bot.reply_to(m, "⛔ Устанавливать плагины может владелец или администратор")
                return
            try:
                dst = self.app.plugins.save_upload(name, data)
                info = self.app.plugins.load(dst)
                if info:
                    self.bot.reply_to(m, f"✅ Плагин <b>{esc(info.name)}</b> установлен.\n"
                                         f"Чтобы хуки инициализации и TG-кнопки заработали полностью — /restart",
                                      reply_markup=kb([[("🔄 Перезапуск", "duo:restart"), ("🧩 Плагины", "duo:plg:0")]]))
                else:
                    self.bot.reply_to(m, "⚠️ Файл сохранён, но плагин не загрузился — смотрите /logs")
            except Exception as e:
                self.bot.reply_to(m, f"❌ {esc(e)}")
        elif name.endswith(".txt"):
            if not self.can(uid, "products"):
                return
            n = Products.add(name, data.decode("utf-8", "ignore").splitlines())
            self.bot.reply_to(m, f"📦 Товары сохранены в <code>{esc(name)}</code> ({n} шт.)")

    def _on_state_text(self, m):
        s = self._own_state(m)
        st, d = s["state"], s["data"]
        text = (m.text or "").strip()
        uid, cid = m.from_user.id, m.chat.id
        self.clear_state(cid, uid, True)
        if text == "/cancel":
            self.bot.reply_to(m, "Отменено")
            return
        c = self.cfg
        if st == "duo_reply":
            acc = d["acc"]
            ok = self.app.send(self._platform(acc), d["chat"], text, account=acc, manual=True)
            self.bot.reply_to(m, "✅ Отправлено" if ok else "❌ Не удалось отправить")
        elif st == "duo_cadd":
            parts = [x.strip() for x in text.split("||")] if "||" in text else [x.strip() for x in text.split("|", 1)]
            if len(parts) < 2:
                self.bot.reply_to(m, "Формат: <code>!команда | ответ</code>")
                return
            cmds = c.get("commands", [])
            cmds.append({"trigger": parts[0], "text": parts[1], "match": "exact", "enabled": True,
                         "platforms": ["funpay", "playerok"], "notify": False})
            c.set("commands", cmds)
            self.send(cid, "✅ Команда добавлена", [[("💬 Автоответы", "duo:cmds")]], uid)
        elif st == "duo_dnew":
            parts = [x.strip() for x in text.split("|")]
            if len(parts) < 2:
                self.bot.reply_to(m, "Формат: <code>ключевое слово | файл.txt | текст с $product</code>")
                return
            rules = c.get("delivery", [])
            rules.append({"keyword": parts[0], "file": Products.safe_name(parts[1]),
                          "text": parts[2] if len(parts) > 2 else "Ваш товар:\n$product", "mode": "file",
                          "per_amount": True, "per_unit": 1, "enabled": True, "cost": 0, "lots": [],
                          "platforms": ["funpay", "playerok"]})
            c.set("delivery", rules)
            self.send(cid, "✅ Правило создано. Себестоимость и привязку лотов можно указать в мини-аппе.",
                      [[("➕ Товары", f"duo:dadd:{len(rules) - 1}"), ("📦 Автовыдача", "duo:dlv")]], uid)
        elif st == "duo_dadd":
            rule = c.get("delivery")[d["i"]]
            n = Products.add(rule["file"], text.splitlines())
            self.bot.reply_to(m, f"✅ В файле <code>{esc(rule['file'])}</code> теперь {n} шт.")
        elif st == "duo_xadd":
            if not parse_proxy(text):
                self.bot.reply_to(m, "Не получилось разобрать прокси — нужны хотя бы IP и порт, например "
                                     "<code>45.91.209.143:10861</code> или <code>ip:порт:логин:пароль</code>")
                return
            p = canonical_proxy(text)
            lst = c.get("telegram.proxy.list", [])
            lst.append(p)
            c.set("telegram.proxy.list", lst)
            self.send(cid, f"Распознал: {esc(describe_proxy(text))}\nСохранил как <code>{esc(proxy_label(p))}</code>\n\n"
                           f"Какой это прокси? Если не знаете — оставьте «авто», бот проверит сам.",
                      self._kind_rows(len(lst) - 1, p) +
                      [[("🔁 Применить", "duo:xapply"), ("🤖 Бот и прокси", "duo:prx")]], uid)
            threading.Thread(target=self._test_and_report, args=(cid, p), daemon=True).start()
        elif st == "duo_addacc":
            self._add_account(m, d["platform"], text)
        elif st == "duo_wdamount":
            try:
                amount = int(float(text.replace(",", ".").replace(" ", "")))
            except ValueError:
                self.bot.reply_to(m, "Нужна сумма числом")
                return
            p = d["platform"]
            plan = self.app.withdraw.plan(p)
            plan["amount"] = amount
            if not plan.get("account"):
                self.bot.reply_to(m, "Аккаунт площадки не подключён")
                return
            self.app.withdraw.ask(p, plan)
        elif st == "duo_edit":
            self._apply_edit(m, d["key"], text)

    def _add_account(self, m, platform, text):
        c = self.cfg
        try:
            self.bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        accs = c.accounts(platform)
        acc_id = c.new_account_id(platform)
        a = {"id": acc_id, "name": "", "enabled": True, "proxy": "", "user_agent": "", "auto_delivery": True}
        if platform == "funpay":
            a.update(golden_key=text.strip(), auto_raise=True)
        elif "token=" in text or ";" in text:
            a.update(cookies=text.strip(), token="")
        else:
            a.update(token=text.strip(), cookies="")
        accs.append(a)
        c.set(f"accounts.{platform}", accs)
        self.app.sync_accounts()
        self.send(m.chat.id, f"✅ Аккаунт <code>{acc_id}</code> добавлен и запускается. Сообщение с ключом удалено.",
                  [[("👤 Открыть", f"duo:acc:{acc_id}")]], m.from_user.id)

    def _apply_edit(self, m, key, text):
        c = self.cfg
        uid = m.from_user.id
        secret = key.endswith(SECRET_EDIT)
        if secret:
            try:
                self.bot.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
        if key.startswith("acc."):
            _, acc_id, field = key.split(".", 2)
            platform = self._platform(acc_id)
            accs = c.accounts(platform)
            note = ""
            if field == "proxy" and text != "-":
                if not parse_proxy(text):
                    self.bot.reply_to(m, "Не получилось разобрать прокси — нужны хотя бы IP и порт, "
                                         "например <code>45.91.209.143:10861</code>")
                    return
                note = f"\nРаспознал: {esc(describe_proxy(text))}"
                text = canonical_proxy(text)
            for a in accs:
                if a["id"] == acc_id:
                    a[field] = "" if text == "-" else text
            c.set(f"accounts.{platform}", accs)
            self.app.sync_accounts()
            self.send(m.chat.id, "✅ Сохранено и применено" + note, [[("👤 Аккаунт", f"duo:acc:{acc_id}")]], uid)
            if field == "proxy" and text and text != "-":
                threading.Thread(target=self._check_acc_proxy, args=(m.chat.id, platform, acc_id, text),
                                 daemon=True).start()
            return
        if key == "general.work_hours.range":
            mm = re.match(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", text)
            if not mm:
                self.bot.reply_to(m, "Формат: 09:00-23:00")
                return
            c.set("general.work_hours.from", mm.group(1).zfill(5))
            c.set("general.work_hours.to", mm.group(2).zfill(5))
        elif key == "telegram.token":
            if not re.match(r"^\d+:[\w-]{30,}$", text):
                self.bot.send_message(m.chat.id, "Это не похоже на токен. Формат: <code>123456:ABC…</code>")
                return
            try:
                me = telebot.TeleBot(text).get_me()
            except Exception as e:
                self.bot.send_message(m.chat.id, f"❌ Токен не работает: {esc(e)}")
                return
            self.bot.send_message(m.chat.id, f"✅ Новый бот: @{esc(me.username)}. Переключаюсь…\n"
                                             f"Напишите ему /start — иначе он не сможет присылать уведомления.")
            c.set("telegram.token", text)
            threading.Thread(target=self.app.reload_telegram, daemon=True).start()
            return
        elif key.startswith("licenses."):
            from . import licensing
            pid = key.split(".", 1)[1]
            licensing.set_key(pid, text if text != "-" else "")
            try:
                self.bot.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
            ok, why = licensing.check(pid, force=True)
            self.send(m.chat.id, ("✅ Ключ принят: " if ok else "❌ Ключ не подошёл: ") + esc(why),
                      [[("🔑 Лицензии", "duo:lic")]], uid)
            return
        elif key == "templates":
            c.set("templates", [x.strip() for x in text.split("\n") if x.strip()])
        elif key == "webhooks":
            c.set("features.webhooks.urls", [x.strip() for x in text.split() if x.startswith("http")])
        elif key == "extra_chats":
            c.set("telegram.extra_chats", [int(x) for x in re.findall(r"-?\d+", text)])
        elif key in ("features.confirm_reminder.after_hours", "features.low_stock.threshold"):
            try:
                c.set(key, float(text.replace(",", ".")) if "hours" in key else int(text))
            except ValueError:
                self.bot.reply_to(m, "Нужно число")
                return
        elif key in ("blacklist.add", "blacklist.remove"):
            bl = c.get("blacklist.users", [])
            for u in [x.strip() for x in re.split(r"[,\n ]", text) if x.strip()]:
                if key.endswith("add") and u not in bl:
                    bl.append(u)
                elif key.endswith("remove") and u in bl:
                    bl.remove(u)
            c.set("blacklist.users", bl)
        elif key.startswith("project.links."):
            c.set(key, "" if text == "-" else text)
            threading.Thread(target=self.update_description, daemon=True).start()
        elif key == "general.watermark":
            c.set(key, text[:60])
            c.set("general.watermark_enabled", True)
        elif isinstance(c.get(key), (int, float)) and not isinstance(c.get(key), bool):
            try:
                v = float(text.replace(",", ".").replace(" ", "").rstrip("%"))
            except ValueError:
                self.bot.reply_to(m, "Нужно число")
                return
            c.set(key, int(v) if isinstance(c.get(key), int) and v.is_integer() else v)
        else:
            c.set(key, text)
            if key == "web.public_url":
                self.app.public_url = text
                self.setup_menu_button()
        hub, sec = self._hub.get(uid), self._sec.get(uid)
        ret = self._ret.get(uid, "")
        if ret.startswith("duo:sec:"):
            t, r = self.section(ret.split(":")[2], uid)
            self.send(m.chat.id, "✅ Сохранено\n\n" + t, r, uid)
        elif ret.startswith("duo:mod:"):
            t, r = self.mod_card(ret.split(":")[2])
            self.send(m.chat.id, "✅ Сохранено\n\n" + t, r, uid)
        elif hub and sec:
            t, r = self.platform_section(hub, sec, uid)
            self.send(m.chat.id, "✅ Сохранено\n\n" + t, r, uid)
        else:
            self.send(m.chat.id, "✅ Сохранено", [[("◀️ Назад", ret or "duo:main")]], uid)

    def _back(self, uid) -> str:
        return self._hub.get(uid) or ("m2" if self._page.get(uid) == "m2" else "main:1")

    def _on_callback(self, c):
        bot = self.bot
        parts = c.data.split(":")
        a = parts[1]
        uid, cid = c.from_user.id, c.message.chat.id
        screens = {"cmds": self.cmds_menu, "dlv": self.dlv_menu, "ntf": self.notify_menu,
                   "prx": self.proxy_menu, "gen": self.general_menu, "feat": self.features_menu,
                   "cust": self.customers_menu, "team": self.team_menu, "wm": self.watermark_menu,
                   "about": self.about_menu, "shops": self.shops_menu, "help": self.help_menu,
                   "webapp": self.webapp_menu}
        if a == "noop":
            bot.answer_callback_query(c.id)
            return
        if a in ("cmds", "dlv"):
            screens[a] = (lambda f, b: (lambda: f(b)))(screens[a], self._back(uid))
        if a in ("main", "m2", "gsw", "sec", "accs", "mods", "mod", "ntf", "cmds", "dlv", "prx", "webapp", "team", "wm", "fin",
                 "plg", "feat", "gen", "cust", "about", "help"):
            self._ret[uid] = c.data  # куда вернуться после правки текста/числа
        if a == "main":
            self._hub.pop(uid, None)
            self._sec.pop(uid, None)
            if len(parts) < 3 and self._page.get(uid) == "m2":
                self.edit(c, *self.menu2(uid))
            else:
                self.edit(c, *self.main_menu(uid))
        elif a == "m2":
            self._hub.pop(uid, None)
            self.edit(c, *self.menu2(uid))
        elif a == "gsw":
            self.edit(c, *self.gsw_menu())
        elif a == "sec":
            self.edit(c, *self.section(parts[2], uid))
        elif a == "accs":
            self.edit(c, *self.accs_menu(uid))
        elif a == "lic":
            self.edit(c, *self.lic_menu())
        elif a == "licchk":
            from . import licensing
            bot.answer_callback_query(c.id, "Проверяю…")
            for d in licensing.describe():
                licensing.check(d["id"], force=True)
            self.edit(c, *self.lic_menu())
            return
        elif a == "mods":
            self.edit(c, *self.mods_menu())
        elif a == "mod":
            key = parts[2]
            m = self.app.modules.get(key)
            sub = parts[3] if len(parts) > 3 else ""
            if m and sub == "sel":
                path = parts[4]
                f = next((x for x in m.FIELDS if x[0] == path), None)
                if f:
                    vals = [v for v, _ in f[3]]
                    cur = m.cfg.get(path)
                    nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
                    self.cfg.set(f"modules.{key}.{path}", nxt)
            elif m and sub in ("act", "free"):
                aid = parts[4] if sub == "act" else "free"
                args = parts[5:] if sub == "act" else parts[4:]
                res = m.run_action(aid, args)
                if len(res) > 180:
                    bot.answer_callback_query(c.id, "Готово — ответ ниже")
                    self.send(cid, f"{m.ICON} <b>{esc(m.NAME)}</b>\n\n{esc(res)[:3800]}", [[("◀️ Модуль", f"duo:mod:{key}")]], uid)
                    return
                bot.answer_callback_query(c.id, res, show_alert=True)
                if sub == "free" and c.message and "Смените пароль" in (getattr(c.message, "text", "") or ""):
                    self.edit(c, (getattr(c.message, "text", "") or "")[:1500] + "\n\n✅ Аккаунт снова в работе", [])
                    return
            self.edit(c, *self.mod_card(key))
        elif a == "set":
            self.edit(c, *self.settings_menu(uid))
        elif a == "tmode":
            m = parts[2]
            self.cfg.set("web.tunnel", m)
            web = self.app.web
            if web and web.tm:
                if m == "none":
                    web.tm.stop()
                    web.tm.info.update(state="off", provider="", error="")
                else:
                    web.tm.reset()
            from .tunnel import MODES
            bot.answer_callback_query(c.id, f"Способ: {MODES.get(m, m).split(' —')[0]}")
            self.edit(c, *self.webapp_menu())
            return
        elif a == "tunnel":
            web = self.app.web
            if not web:
                bot.answer_callback_query(c.id, "Веб-панель выключена", show_alert=True)
                return
            if parts[2] == "restart":
                web.restart_tunnel()
                bot.answer_callback_query(c.id, "Пробую другой способ… Когда заработает — пришлю сообщение.")
            else:
                bot.answer_callback_query(c.id, "Проверяю…")
                web.check_tunnel()
            self.edit(c, *self.webapp_menu())
            return
        elif a in screens:
            self.edit(c, *screens[a]())
        elif a == "fin" or a == "stats":
            self.edit(c, *self.finance_menu(uid))
        elif a in ("fp", "pl"):
            self._hub[uid] = a
            self._sec.pop(uid, None)
            self.edit(c, *self.platform_menu(a, uid))
        elif a == "ps":
            self._hub[uid] = parts[2]
            self._sec[uid] = parts[3]
            self.edit(c, *self.platform_section(parts[2], parts[3], uid))
        elif a == "wdm":
            p, mode = parts[2], parts[3]
            self.cfg.set(f"withdraw.{'funpay' if p == 'fp' else 'playerok'}.mode", mode)
            bot.answer_callback_query(c.id, {"off": "Автовывод выключен", "ask": "Буду спрашивать перед выводом",
                                             "auto": "Вывод автоматически по правилам"}[mode])
            self.edit(c, *(self.section("wd", uid) if self._ret.get(uid, "").startswith("duo:sec:wd")
                           else self.platform_section(p, "wd", uid)))
            return
        elif a == "oos":
            p, field, val = parts[2], parts[3], parts[4]
            o = dict(self.cfg.get("features.out_of_stock", {}))
            o[field] = val
            self.cfg.set("features.out_of_stock", o)
            self.edit(c, *(self.section("lots", uid) if self._ret.get(uid, "").startswith("duo:sec:lots")
                           else self.platform_section(p, "lots", uid)))
        elif a == "status":
            self.edit(c, self.status_text(uid), [[("🔄 Обновить", "duo:status"), ("🏠 Главное меню", "duo:main")]])
        elif a == "t":
            path, back = parts[2], parts[3]
            if path.startswith(("telegram.proxy",)) and not self.can(uid, "system"):
                bot.answer_callback_query(c.id, "⛔ Недостаточно прав", show_alert=True)
                return
            v = self.cfg.toggle(path)
            bot.answer_callback_query(c.id, "Включено" if v else "Выключено")
            if path == "project.set_bot_description" and v:
                threading.Thread(target=self.update_description, daemon=True).start()
            if path.endswith(("funpay.enabled", "playerok.enabled")):
                self.app.sync_accounts()
            if back == "main":
                self.edit(c, *self.main_menu(uid))
            elif back == "gsw":
                self.edit(c, *self.gsw_menu())
            elif back.startswith("sec-"):
                self.edit(c, *self.section(back[4:], uid))
            elif back.startswith("mod-"):
                self.edit(c, *self.mod_card(back[4:]))
            elif back == "set":
                self.edit(c, *self.settings_menu(uid))
            elif back in ("fp", "pl"):
                self.edit(c, *self.platform_menu(back, uid))
            elif "-" in back:
                bp, bs = back.split("-", 1)
                self.edit(c, *self.platform_section(bp, bs, uid))
            else:
                self.edit(c, *screens[back]())
            return
        elif a == "e":
            key = ":".join(parts[2:])
            if key.endswith(SECRET_EDIT) or key == "telegram.token":
                if not self.can(uid, "secrets"):
                    bot.answer_callback_query(c.id, "⛔ Ключи и пароли меняет только владелец", show_alert=True)
                    return
            hints = {"general.work_hours.range": "Введите часы работы, например <code>09:00-23:00</code>",
                     "telegram.token": "🤖 Отправьте токен нового бота от @BotFather. Сообщение с токеном я удалю.",
                     "templates": "Отправьте шаблоны ответов — каждый с новой строки",
                     "blacklist.add": "Ники для ЧС (через запятую)", "blacklist.remove": "Ники для удаления из ЧС",
                     "webhooks": "Адреса вебхуков, каждый с новой строки",
                     "extra_chats": "ID групп/каналов для уведомлений через пробел. Пусто — никуда, кроме команды.",
                     "features.daily_report.time": "Время отчёта, например <code>23:55</code>",
                     "features.vacation.until": "До какой даты отпуск? Формат <code>2026-12-31</code>",
                     "general.watermark": "Отправьте свою подпись (до 60 символов), например <code>♊ 𝑪𝒂𝒔𝒕𝒐𝒓</code>",
                     **{f"project.links.{k}": "Отправьте ссылку: <code>@username</code> или <code>https://…</code>. "
                                              "«-» — убрать ссылку." for k in ("updates", "plugins", "chat", "donate", "github")}}
            if key.startswith("licenses."):
                self.ask(cid, uid, "duo_edit", "🔑 Отправьте ключ лицензии (CST-XXXX-…). Сообщение с ключом я удалю.",
                         {"key": key})
                return
            if key.startswith("acc."):
                field = key.split(".", 2)[2]
                hint = {"name": "Название аккаунта (видно только вам)", "proxy": "Прокси для аккаунта, «-» — без прокси",
                        "golden_key": "Новый golden_key. Сообщение я удалю.",
                        "token": "Новый token Playerok (или все cookies строкой). Сообщение я удалю."}.get(field, field)
            else:
                cur = self.cfg.get(key)
                show = "" if key.endswith(SECRET_EDIT) or not isinstance(cur, str) else f"\nСейчас: <code>{esc(cur)}</code>"
                num = isinstance(cur, (int, float)) and not isinstance(cur, bool)
                hint = hints.get(key, ("✏️ Пришлите новое " + ("число" if num else "значение") + " одним сообщением."
                                       + show + ("" if num else "\n\n<i>Можно использовать: $username $order_id "
                                                                   "$order_title $product $date</i>")))
            self.ask(cid, uid, "duo_edit", hint, {"key": key})
        elif a == "wmp":
            self.cfg.set("general.watermark", WATERMARK_PRESETS[int(parts[2])])
            self.cfg.set("general.watermark_enabled", True)
            bot.answer_callback_query(c.id, "Подпись выбрана")
            self.edit(c, *self.watermark_menu())
            return
        elif a == "acc":
            self.edit(c, *self.account_card(parts[2]))
        elif a == "acct":
            acc_id, field = parts[2], parts[3]
            platform = self._platform(acc_id)
            accs = self.cfg.accounts(platform)
            for x in accs:
                if x["id"] == acc_id:
                    x[field] = not x.get(field, True)
            self.cfg.set(f"accounts.{platform}", accs)
            self.app.sync_accounts()
            self.edit(c, *self.account_card(acc_id))
        elif a == "addacc":
            platform = "funpay" if parts[2] == "fp" else "playerok"
            self.ask(cid, uid, "duo_addacc",
                     "Отправьте golden_key нового аккаунта FunPay (funpay.com → F12 → Application → Cookies)."
                     if platform == "funpay" else
                     "Отправьте token нового аккаунта Playerok (или все cookies строкой).", {"platform": platform})
        elif a == "accdel":
            bot.send_message(cid, f"🗑 Удалить аккаунт <code>{parts[2]}</code> из бота? Статистика сохранится.",
                             reply_markup=kb([[("Да, удалить", f"duo:accdel2:{parts[2]}"), ("Нет", f"duo:acc:{parts[2]}")]]))
        elif a == "accdel2":
            acc_id = parts[2]
            platform = self._platform(acc_id)
            self.cfg.set(f"accounts.{platform}", [x for x in self.cfg.accounts(platform) if x["id"] != acc_id])
            self.app.sync_accounts()
            self.edit(c, f"✅ Аккаунт {acc_id} удалён", [[("◀️ Аккаунты", "duo:accs")]])
        elif a == "reply":
            acc, chat = self._resolve_acc(parts[2]), ":".join(parts[3:])
            self.ask(cid, uid, "duo_reply", f"✍️ Ответ ({'FunPay' if self._platform(acc) == 'funpay' else 'Playerok'}"
                                            f"{self.app.acc_tag(acc)}):", {"acc": acc, "chat": self._chat(chat)})
        elif a == "tpl":
            acc, chat = self._resolve_acc(parts[2]), ":".join(parts[3:])
            rows = [[(t[:40], f"duo:tps:{acc}:{i}:{chat}")] for i, t in enumerate(self.cfg.get("templates", []))]
            bot.send_message(cid, "📝 Выберите шаблон:", reply_markup=kb(rows or [[("Шаблонов нет", "duo:main")]]))
        elif a == "tps":
            acc, i, chat = self._resolve_acc(parts[2]), int(parts[3]), ":".join(parts[4:])
            t = self.cfg.get("templates", [])[i]
            ok = self.app.send(self._platform(acc), self._chat(chat), t, account=acc, manual=True)
            bot.answer_callback_query(c.id, "Отправлено" if ok else "Ошибка")
            return
        elif a == "refund":
            acc, oid = (self._resolve_acc(parts[2]), parts[3]) if len(parts) > 3 else (self._resolve_acc("fp"), parts[2])
            bot.send_message(cid, f"💸 Вернуть деньги по заказу <code>#{esc(oid)}</code>?",
                             reply_markup=kb([[("✅ Да, вернуть", f"duo:refund2:{acc}:{oid}"), ("❌ Нет", "duo:main")]]))
        elif a == "refund2":
            acc, oid = parts[2], parts[3]
            svc = self.app.service("funpay", acc)
            try:
                svc.client.refund(oid)
                self.app.stats.inc("refunds")
                self.app.stats.set_order_flag(oid, "refunded", True, "funpay")
                log.info("💸 Возврат %s (%s) — %s", oid, acc, uid)
                self.edit(c, f"✅ Возврат по <code>#{esc(oid)}</code> выполнен", [[("◀️ Меню", "duo:main")]])
            except Exception as e:
                self.edit(c, f"❌ Возврат не удался: {esc(e)}", [[("◀️ Меню", "duo:main")]])
        elif a == "plsent":
            acc, did = (self._resolve_acc(parts[2]), parts[3]) if len(parts) > 3 else (self._resolve_acc("pl"), parts[2])
            try:
                self.app.service("playerok", acc).bridge.mark_sent(did)
                bot.answer_callback_query(c.id, "Отмечено отправленным")
            except Exception as e:
                bot.answer_callback_query(c.id, f"Ошибка: {e}"[:190], show_alert=True)
            return
        elif a == "manual":
            oid = parts[-1]
            o = self._find_order(oid)
            if not o or not o.get("chat_id"):
                bot.answer_callback_query(c.id, "Не нашёл заказ или чат покупателя", show_alert=True)
                return
            uo = UOrder(o["platform"], o["id"], o["title"], o.get("price") or 0, o["buyer"], None,
                        self._chat(o["chat_id"]), o.get("amount", 1), account=o.get("account"))
            uo._app = self.app
            ok = self.app.deliver(uo)
            bot.answer_callback_query(c.id, "Выдано" if ok else "Не выдано: нет правила или товара", show_alert=not ok)
            return
        elif a == "stk":
            res = self.app.stock.resolve(parts[2], parts[3] == "y")
            self.edit(c, (getattr(c.message, "text", "") or "")[:1500] + f"\n\n{res}", [])
        elif a == "wd":
            res = self.app.withdraw.resolve(parts[2], parts[3] == "y", by_uid=uid)
            self.edit(c, (getattr(c.message, "text", "") or "")[:1500] + f"\n\n{esc(res)}", [])
        elif a == "wdnow":
            p = parts[2]
            plan = self.app.withdraw.plan(p)
            w = self.cfg.get(f"withdraw.{p}", {})
            if not w.get("wallet"):
                bot.answer_callback_query(c.id, "Сначала укажите реквизиты в мини-аппе (Финансы → Автовывод)", show_alert=True)
                return
            self.ask(cid, uid, "duo_wdamount", f"Доступно: {plan['available']}{'' if plan['exact'] else ' (приблизительно)'}\n"
                                               f"По правилам можно вывести: {plan['amount'] or '—'} {esc(plan['reason'])}\n\n"
                                               f"Введите сумму вывода — я пришлю кнопку подтверждения.", {"platform": p})
        elif a == "wdun":
            self.app.withdraw.unpause(parts[2])
            bot.answer_callback_query(c.id, "Пауза снята")
            self.edit(c, *self.finance_menu(uid))
            return
        elif a == "raise":
            self._do_raise(cid)
        elif a == "fpbal":
            lines = []
            for svc in self.app.fp.values():
                try:
                    svc.client.login()
                    lines.append(f"{self.app.account_name(svc.acc_id)}: {svc.client.balance} {svc.client.currency}")
                except Exception as e:
                    lines.append(f"{svc.acc_id}: ошибка {e}")
            bot.answer_callback_query(c.id, "\n".join(lines)[:190] or "Нет аккаунтов", show_alert=True)
            return
        elif a == "cdel":
            cmds = self.cfg.get("commands", [])
            i = int(parts[2])
            if 0 <= i < len(cmds):
                cmds.pop(i)
                self.cfg.set("commands", cmds)
            self.edit(c, *self.cmds_menu(self._back(uid)))
        elif a == "cntf":
            cmds = self.cfg.get("commands", [])
            i = int(parts[2])
            cmds[i]["notify"] = not cmds[i].get("notify")
            self.cfg.set("commands", cmds)
            self.edit(c, *self.cmds_menu(self._back(uid)))
        elif a == "cadd":
            self.ask(cid, uid, "duo_cadd", "Отправьте: <code>!команда | ответ</code>\n"
                                           "Несколько триггеров: <code>!цена|!прайс || Цены в описании</code>")
        elif a == "dnew":
            self.ask(cid, uid, "duo_dnew", "Отправьте: <code>ключевое слово | файл.txt | текст выдачи</code>\n"
                                           "Пример: <code>Steam ключ | steam.txt | Ваш ключ: $product</code>")
        elif a == "dadd":
            self.ask(cid, uid, "duo_dadd", "Отправьте товары (каждый с новой строки) или .txt файлом:", {"i": int(parts[2])})
        elif a == "dtog":
            rules = self.cfg.get("delivery", [])
            i = int(parts[2])
            rules[i]["enabled"] = not rules[i].get("enabled", True)
            self.cfg.set("delivery", rules)
            self.edit(c, *self.dlv_menu(self._back(uid)))
        elif a == "ddel":
            rules = self.cfg.get("delivery", [])
            rules.pop(int(parts[2]))
            self.cfg.set("delivery", rules)
            self.edit(c, *self.dlv_menu(self._back(uid)))
        elif a == "plg":
            self.edit(c, *self.plugins_menu(int(parts[2]) if len(parts) > 2 else 0))
        elif a == "pi":
            self.edit(c, *self.plugin_card(parts[2]))
        elif a == "ptog":
            v = self.app.plugins.toggle(parts[2])
            bot.answer_callback_query(c.id, "Включён" if v else "Выключен")
            self.edit(c, *self.plugin_card(parts[2]))
            return
        elif a == "pdel":
            bot.send_message(cid, "🗑 Удалить плагин?", reply_markup=kb([[("Да", f"duo:pdel2:{parts[2]}"),
                                                                          ("Нет", f"duo:pi:{parts[2]}")]]))
        elif a == "pdel2":
            ok = self.app.plugins.delete(parts[2])
            self.edit(c, "✅ Удалён (полностью — после /restart)" if ok else "Не найден", [[("🧩 Плагины", "duo:plg:0")]])
        elif a == "inv":
            role = parts[2]
            if role not in ROLES:
                return
            code = self.app.team.invite(role, uid)
            link = f"https://t.me/{self.username}?start=inv_{code}"
            bot.send_message(cid, f"🔗 Приглашение: <b>{ROLE_NAMES[role]}</b>\n{ROLE_HINTS[role]}\n\n"
                                  f"<code>{link}</code>\n\nОдноразовое, действует 24 часа. Отправьте ссылку помощнику.")
        elif a == "usr":
            self.edit(c, *self.user_card(int(parts[2])))
        elif a == "urole":
            ok = self.app.team.set_role(int(parts[2]), parts[3])
            bot.answer_callback_query(c.id, "Роль изменена" if ok else "Нельзя: должен остаться хотя бы один владелец",
                                      show_alert=not ok)
            self.edit(c, *self.user_card(int(parts[2])))
            return
        elif a == "udel":
            ok = self.app.team.remove(int(parts[2]))
            bot.answer_callback_query(c.id, "Удалён" if ok else "Нельзя удалить последнего владельца", show_alert=not ok)
            self.edit(c, *self.team_menu())
            return
        elif a == "xadd":
            self.ask(cid, uid, "duo_xadd", "Отправьте прокси в любом виде — как дал продавец:\n"
                                           "<code>ip:порт:логин:пароль</code> · <code>логин:пароль@ip:порт</code> · "
                                           "<code>socks5://логин:пароль@ip:порт</code> · IPv6 · или блоком «IP / Порт / "
                                           "Логин / Пароль».\n<i>MTProto-прокси не подходят для Bot API.</i>")
        elif a in ("xkinds", "xkind"):
            i = int(parts[2])
            lst = self.cfg.get("telegram.proxy.list", [])
            if i >= len(lst):
                return
            if a == "xkind":
                old = lst[i]
                lst[i] = canonical_proxy(old, parts[3])
                self.cfg.set("telegram.proxy.list", lst)
                if self.app.proxy.current == old:
                    self.app.proxy.current = lst[i]
                bot.answer_callback_query(c.id, f"Тип: {PROXY_KINDS[parts[3]]}")
            p = lst[i]
            self.edit(c, f"<b>🏷 Тип прокси</b> <code>{esc(proxy_label(p))}</code>\n{esc(describe_proxy(p))}\n\n"
                         "IPv4 — обычный личный · IPv4 Shared — общий на несколько человек · "
                         "IPv6 — только для сайтов с IPv6 (Telegram подходит)",
                      self._kind_rows(i, p) + [[("◀️ Назад", "duo:prx")]])
            return
        elif a == "xtest":
            p = self.cfg.get("telegram.proxy.list", [])[int(parts[2])]
            ok, info = self.app.proxy.test(p)
            bot.answer_callback_query(c.id, f"✅ {info} мс" if ok else f"❌ {info}"[:190], show_alert=True)
            return
        elif a == "xdel":
            lst = self.cfg.get("telegram.proxy.list", [])
            lst.pop(int(parts[2]))
            self.cfg.set("telegram.proxy.list", lst)
            self.edit(c, *self.proxy_menu())
        elif a == "xtoggle":
            v = self.cfg.toggle("telegram.proxy.enabled")
            bot.answer_callback_query(c.id, "Прокси включён, применяю…" if v else "Прокси выключен, применяю…")
            threading.Thread(target=self._apply_and_report, args=(cid,), daemon=True).start()
            return
        elif a == "xuse":
            lst = self.cfg.get("telegram.proxy.list", [])
            lst.insert(0, lst.pop(int(parts[2])))
            self.cfg.set("telegram.proxy.list", lst)
            self.cfg.set("telegram.proxy.enabled", True)
            bot.answer_callback_query(c.id, "Применяю прокси…")
            threading.Thread(target=self._apply_and_report, args=(cid,), daemon=True).start()
            return
        elif a == "xapply":
            bot.answer_callback_query(c.id, "Переподключаюсь…")
            threading.Thread(target=self._apply_and_report, args=(cid,), daemon=True).start()
            return
        elif a == "xall":
            bot.answer_callback_query(c.id, "Проверяю…")
            res = self.app.proxy.test_all()
            bot.send_message(cid, "🧪 <b>Проверка прокси</b>\n" + "\n".join(
                f"{'✅' if r['ok'] else '❌'} <code>{esc(r['label'])}</code> — {esc(r['info'])}{' мс' if r['ok'] else ''}"
                for r in res), reply_markup=kb([[("🤖 Бот и прокси", "duo:prx")]]))
            return
        elif a == "logs":
            self._send_logs(cid)
        elif a == "backup":
            path = make_backup()
            with open(path, "rb") as f:
                bot.send_document(cid, f, caption="💾 Бекап: конфиг (с ключами!), товары, статистика, плагины. "
                                                  "Не пересылайте его посторонним.", visible_file_name=path.name)
        elif a == "restart":
            bot.send_message(cid, "🔄 Перезапускаюсь…")
            threading.Thread(target=self.app.restart, daemon=True).start()
        elif a == "cancel":
            self.clear_state(cid, uid, True)
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass
