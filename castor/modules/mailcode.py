"""Коды с почты: покупатель пишет !код — бот заходит в нужный почтовый ящик (IMAP), находит свежее письмо
с кодом и присылает только код. Пароль от почты покупатель не видит.

Какой ящик: у арендованного аккаунта (поле «Почтовый ящик» в аренде) или по слову в названии заказа,
который покупатель недавно оплатил.
"""
from __future__ import annotations

import email
import html as htmllib
import imaplib
import re
import threading
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime

from ..core import esc
from . import Module


def _dec(v) -> str:
    out = ""
    for part, enc in decode_header(v or ""):
        out += part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part
    return out


def _body(msg) -> str:
    texts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            t = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/html":
            t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
            t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
        texts.append(t)
    return re.sub(r"[ \t\r\f\v]+", " ", "\n".join(texts))


def fetch_code(box: dict, max_age_min: float = 20) -> tuple:
    """→ (код | None, пояснение). Смотрит последние письма в INBOX."""
    host = box.get("host", "").strip()
    if not host:
        return None, "не указан IMAP-сервер"
    rx = re.compile(box.get("code_regex") or r"\b([A-Z0-9]{5}|\d{4,8})\b")
    frm = (box.get("from_filter") or "").lower().strip()
    subj = (box.get("subject_filter") or "").lower().strip()
    try:
        im = imaplib.IMAP4_SSL(host, int(box.get("port") or 993), timeout=25)
    except Exception as e:
        return None, f"нет связи с почтовым сервером ({type(e).__name__})"
    try:
        try:
            im.login(box.get("login", ""), box.get("password", ""))
        except imaplib.IMAP4.error:
            return None, "почта не пустила: неверный логин или пароль приложения"
        im.select("INBOX", readonly=True)
        typ, data = im.search(None, "ALL")
        ids = (data[0].split() if data and data[0] else [])[-15:][::-1]
        for i in ids:
            typ, msgdata = im.fetch(i, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            try:
                age = time.time() - parsedate_to_datetime(msg.get("Date")).timestamp()
            except Exception:
                age = 0
            if age > max_age_min * 60:
                break  # дальше только старые
            if frm and frm not in _dec(msg.get("From")).lower():
                continue
            if subj and subj not in _dec(msg.get("Subject")).lower():
                continue
            m = rx.search(_dec(msg.get("Subject")) + "\n" + _body(msg))
            if m:
                return (m.group(1) if m.groups() else m.group(0)), ""
        return None, "свежего письма с кодом пока нет"
    finally:
        try:
            im.logout()
        except Exception:
            pass


class MailCodeModule(Module):
    KEY = "mailcode"
    GROUP = "Выдача"
    ORDER = 40
    NAME = "EmailCode"
    ICON = "📧"
    DESCRIPTION = "Покупатель пишет !код — бот берёт свежий код подтверждения из нужной почты (IMAP) и присылает его."
    DEFAULTS = {
        "command": "!код|!code|!почта",
        "max_age_min": 20,
        "order_days": 30,
        "mailboxes": [],   # [{"name", "host", "port", "login", "password", "from_filter", "subject_filter", "code_regex", "keyword"}]
        "text_code": "Ваш код: $code",
        "text_none": "Свежего кода пока нет. Запросите код на сайте и напишите !код ещё раз через минуту.",
        "text_denied": "Код можно получить только по оплаченному заказу или активной аренде.",
    }
    FIELDS = [
        ("mailboxes", "Почтовые ящики", "items"),
        ("command", "Команда покупателя", "text"),
        ("max_age_min", "Искать письма не старше, мин", "number"),
        ("order_days", "Заказ должен быть не старше, дней", "number"),
        ("text_code", "Сообщение с кодом", "area", "$code — код"),
        ("text_none", "Если кода нет", "area"),
        ("text_denied", "Если нет заказа", "area"),
    ]
    ITEM_FIELDS = {
        "mailboxes": [("name", "Имя ящика", "text", "по нему ящик связывается с арендой"),
                      ("host", "IMAP-сервер", "text", "imap.gmail.com · imap.mail.ru · imap.yandex.ru · outlook.office365.com"),
                      ("port", "Порт", "number"), ("login", "Логин (почта)", "text"),
                      ("password", "Пароль приложения", "secret", "не обычный пароль, а «пароль для приложений» почты"),
                      ("keyword", "Слово в названии заказа", "text", "кому из покупателей можно отдавать коды"),
                      ("from_filter", "Письма от (часть адреса)", "text", "например steampowered.com"),
                      ("subject_filter", "Тема содержит", "text"),
                      ("code_regex", "Как выглядит код (regex)", "text", "по умолчанию 5 символов или 4–8 цифр")],
    }

    def __init__(self, app):
        super().__init__(app)
        self._last: dict = {}

    def _box_for(self, m):
        boxes = self.get("mailboxes", [])
        from .rent import RentBase
        for rent in (self.app.modules.active() if getattr(self.app, "modules", None) else []):
            if not isinstance(rent, RentBase):
                continue
            rid, r = rent.active_for(m.platform, m.author)
            if r:
                acc = rent.account(r["account_id"]) or {}
                b = next((x for x in boxes if x.get("name") and x.get("name") == acc.get("mailbox")), None)
                if b:
                    return b
        since = time.time() - float(self.get("order_days", 30) or 30) * 86400
        mine = [o for o in self.app.stats.data.get("orders", []) if o.get("p") == m.platform and o.get("t", 0) >= since
                and str(o.get("buyer", "")).lower() == str(m.author).lower() and not o.get("refunded")]
        for o in reversed(mine):
            title = (o.get("title") or "").lower()
            for b in boxes:
                kws = [k.strip().lower() for k in str(b.get("keyword", "")).split("|") if k.strip()]
                if kws and any(k in title for k in kws):
                    return b
        return None

    def on_message(self, m) -> bool:
        t = (m.text or "").strip().lower()
        if not any(t == c.strip().lower() for c in str(self.get("command", "")).split("|") if c.strip()):
            return False
        key = f"{m.platform}:{m.author}"
        if time.time() - self._last.get(key, 0) < 20:
            self.reply(m.platform, m.chat_id, "Подождите 20 секунд перед следующим запросом.", m.account, m.chat_name)
            return True
        self._last[key] = time.time()
        box = self._box_for(m)
        if not box:
            self.reply(m.platform, m.chat_id, self.get("text_denied"), m.account, m.chat_name)
            return True

        def work():
            code, why = fetch_code(box, float(self.get("max_age_min", 20) or 20))
            if code:
                self.reply(m.platform, m.chat_id, self.get("text_code", "Код: $code").replace("$code", code),
                           m.account, m.chat_name)
                self.notify(f"📧 Код с почты «{esc(box.get('name'))}» отправлен {esc(m.author)}")
            else:
                self.reply(m.platform, m.chat_id, self.get("text_none"), m.account, m.chat_name)
                if "пароль" in why or "связи" in why or "сервер" in why:
                    self.notify(f"📧 Почта «{esc(box.get('name'))}»: {esc(why)}", kind="errors")
        threading.Thread(target=work, daemon=True).start()
        return True

    def status(self) -> str:
        return f"ящиков: {len(self.get('mailboxes', []))}"

    def actions(self) -> list:
        return [(f"test:{i}", f"🧪 Проверить «{b.get('name') or b.get('login')}»") for i, b in enumerate(self.get("mailboxes", []))]

    def run_action(self, action, args) -> str:
        if action == "test" and args:
            boxes = self.get("mailboxes", [])
            i = int(args[0])
            if i >= len(boxes):
                return "Нет такого ящика"
            code, why = fetch_code(boxes[i], max_age_min=24 * 60)
            return f"Подключение есть. Последний код за сутки: {code}" if code else f"Результат: {why}"
        return "Нет такого действия"
