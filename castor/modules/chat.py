"""Модули общения: перевод сообщений, антиспам и защита от увода, опрос после покупки."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

import requests

from ..core import esc, fmt_template
from . import Module


# ═════════════════════ перевод ═════════════════════
def translate(text: str, to: str = "ru", src: str = "auto") -> tuple:
    """→ (перевод, язык оригинала). Бесплатный публичный переводчик Google; при ошибке — (None, '')."""
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": src, "tl": to, "dt": "t", "q": text[:4500]}, timeout=8)
        j = r.json()
        out = "".join(part[0] for part in j[0] if part and part[0])
        return out, (j[2] if len(j) > 2 and isinstance(j[2], str) else src)
    except Exception:
        return None, ""


LANG_NAMES = {"en": "английский", "uk": "украинский", "kk": "казахский", "de": "немецкий", "es": "испанский",
              "pt": "португальский", "tr": "турецкий", "pl": "польский", "uz": "узбекский", "be": "белорусский",
              "fr": "французский", "it": "итальянский", "zh-CN": "китайский", "ar": "арабский", "vi": "вьетнамский"}


class TranslateModule(Module):
    KEY = "translate"
    GROUP = "Общение"
    ORDER = 3
    NAME = "Перевод сообщений"
    ICON = "🌐"
    DESCRIPTION = ("Покупатель пишет не по-русски — бот присылает вам перевод. Ваши ответы из Telegram и панели "
                   "переводит на язык покупателя.")
    DEFAULTS = {"translate_replies": True, "min_len": 4}
    FIELDS = [("translate_replies", "Переводить мои ответы на язык покупателя", "bool"),
              ("min_len", "Не переводить сообщения короче, символов", "number")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("lang", {})     # "platform:chat_id" → язык
        self.data.setdefault("count", 0)

    @staticmethod
    def _foreign(text: str) -> bool:
        letters = re.findall(r"[^\W\d_]", text or "")
        if not letters:
            return False
        cyr = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
        return cyr / len(letters) < 0.3

    def on_message(self, m) -> bool:
        txt = (m.text or "").strip()
        if m.system or m.by_me or len(txt) < int(self.get("min_len", 4) or 4) or not self._foreign(txt) or txt.startswith("!"):
            return False

        def work():
            tr, lang = translate(txt, "ru")
            if not tr or lang in ("ru", ""):
                return
            self.data["lang"][f"{m.platform}:{m.chat_id}"] = lang
            self.data["count"] += 1
            self.save()
            self.notify(f"🌐 <b>{esc(m.author)}</b> пишет на языке «{esc(LANG_NAMES.get(lang, lang))}»:\n"
                        f"<i>{esc(tr)[:3000]}</i>"
                        + ("\n\nВаш ответ из Telegram бот переведёт сам." if self.get("translate_replies", True) else ""),
                        buttons=[[("↩️ Ответить", f"duo:reply:{m.account}:{m.chat_id}", "chat")]], kind="fp_message")
        threading.Thread(target=work, daemon=True).start()
        return False

    def on_send(self, platform, chat_id, text, manual) -> str:
        if not manual or not self.get("translate_replies", True):
            return text
        lang = self.data["lang"].get(f"{platform}:{chat_id}")
        if not lang or lang == "ru":
            return text
        tr, _ = translate(text, lang, "ru")
        return tr or text

    def status(self) -> str:
        return f"переведено сообщений: {self.data['count']}"


# ═════════════════════ антиспам и защита от увода ═════════════════════
DEFAULT_PATTERNS = [
    r"(?i)\b(t\.me|telegram\.me|wa\.me|vk\.com|discord\.gg|discord\.com/invite)\b",
    r"(?i)(пиши\w*|напиш\w*|добав\w*|го|перейд\w*|свяж\w*|стучи\w*)\s+(мне\s+)?(в|во)\s+(тг|телег\w*|tg|telegram|ватсап\w*|whatsapp|дискорд\w*|discord|вк|vk|вайбер\w*|viber)\b",
    r"(?i)\b(мой|мой ник в|мой контакт в)\s+(тг|телег\w*|tg|telegram|дискорд\w*|discord)\b",
    r"(?i)(вне площадки|без комиссии|напрямую|в обход|на карту|переведи(те)? на|оплат\w* (на )?карт)",
    r"(?<!\d)(?:\d[ -]?){16}(?!\d)",           # номер карты
    r"(?<!\d)(?:\+7|8)[ (-]*\d{3}[ )-]*\d{3}[ -]*\d{2}[ -]*\d{2}(?!\d)",   # телефон
    r"(?i)https?://(?!funpay\.com|playerok\.com)\S+",
]


class AntiSpamModule(Module):
    KEY = "antispam"
    GROUP = "Общение"
    ORDER = 2
    NAME = "Антиспам и защита"
    ICON = "🛡"
    DESCRIPTION = ("Предупреждает, когда покупатель зовёт в Telegram, просит оплату на карту, кидает ссылки или телефон "
                   "(за такое FunPay может забанить и вас). Глушит автоответы флудерам.")
    DEFAULTS = {"warn_owner": True, "warn_buyer": False,
                "text_buyer": "Общение и оплата — только через площадку, так безопаснее для нас обоих 🙏",
                "flood_per_min": 8, "mute_min": 10, "extra_patterns": []}
    FIELDS = [("warn_owner", "Сообщать мне о подозрительных сообщениях", "bool"),
              ("warn_buyer", "Отвечать покупателю предупреждением", "bool"),
              ("text_buyer", "Текст предупреждения", "area"),
              ("flood_per_min", "Флуд: больше сообщений в минуту", "number"),
              ("mute_min", "Не отвечать флудеру, мин", "number"),
              ("extra_patterns", "Свои стоп-слова или regex (по строке)", "lines")]

    def __init__(self, app):
        super().__init__(app)
        self._hits = defaultdict(lambda: deque(maxlen=60))
        self._muted: dict = {}
        self._warned: dict = {}
        self.data.setdefault("caught", 0)

    def on_message(self, m) -> bool:
        if m.by_me or m.system:
            return False
        key = f"{m.platform}:{m.account}:{m.chat_id}"
        now = time.time()
        if self._muted.get(key, 0) > now:
            return True
        q = self._hits[key]
        q.append(now)
        lim = int(self.get("flood_per_min", 8) or 0)
        if lim and sum(1 for t in q if now - t < 60) > lim:
            self._muted[key] = now + float(self.get("mute_min", 10) or 10) * 60
            self.notify(f"🛡 <b>{esc(m.author)}</b> флудит — автоответы ему выключены на {self.get('mute_min')} мин",
                        kind="errors")
            return True
        pats = DEFAULT_PATTERNS + [p if any(c in p for c in "\\[]()?*+") else re.escape(p)
                                   for p in self.get("extra_patterns", []) if str(p).strip()]
        for p in pats:
            try:
                hit = re.search(p, m.text or "")
            except re.error:
                continue
            if hit:
                self.data["caught"] += 1
                self.save()
                if self.get("warn_owner", True):
                    self.notify(f"🛡 <b>Подозрительное сообщение</b> от {esc(m.author)}: «{esc(hit.group(0))}»\n"
                                f"<i>{esc(m.text)[:500]}</i>\n\nНе уходите общаться и получать оплату вне площадки.",
                                buttons=[[("↩️ Ответить", f"duo:reply:{m.account}:{m.chat_id}", "chat")]], kind="errors")
                if self.get("warn_buyer") and now - self._warned.get(key, 0) > 3600:
                    self._warned[key] = now
                    self.reply(m.platform, m.chat_id, self.get("text_buyer"), m.account, m.chat_name)
                break
        return False

    def status(self) -> str:
        return f"поймано подозрительных: {self.data['caught']}"


# ═════════════════════ опрос после покупки ═════════════════════
class SurveyModule(Module):
    KEY = "survey"
    GROUP = "Общение"
    ORDER = 10
    NAME = "Опрос после покупки"
    ICON = "📝"
    DESCRIPTION = ("Через несколько часов после выдачи спрашивает «как всё прошло, оцените от 1 до 5». Довольных просит "
                   "оставить отзыв, недовольных — сразу передаёт вам, пока они не написали плохой отзыв.")
    DEFAULTS = {"delay_hours": 2, "good_from": 4,
                "text_ask": "Здравствуйте, $username! Как всё прошло с заказом «$order_title»? Оцените, пожалуйста, от 1 до 5 🙏",
                "text_good": "Спасибо! 🙌 Будем очень благодарны за отзыв на заказе — это нам сильно помогает ⭐",
                "text_bad": "Очень жаль 😔 Напишите, что не так, — продавец уже получил уведомление и скоро ответит."}
    FIELDS = [("delay_hours", "Спросить через, ч после выдачи", "number"),
              ("good_from", "Хорошая оценка — от", "number"),
              ("text_ask", "Вопрос", "area", "$username $order_title $order_id"),
              ("text_good", "Ответ довольному", "area"), ("text_bad", "Ответ недовольному", "area")]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("since", time.time())
        self.data.setdefault("asked", {})     # order_id → t
        self.data.setdefault("pending", {})   # "platform:buyer" → {...}
        self.data.setdefault("scores", [])
        self._last = 0

    def tick(self):
        if time.time() - self._last < 120:
            return
        self._last = time.time()
        delay = float(self.get("delay_hours", 2) or 2) * 3600
        with self.lock:
            for o in self.app.stats.data.get("orders", [])[-300:]:
                if o.get("t", 0) < self.data["since"] or not o.get("delivered") or o.get("refunded"):
                    continue
                if o["id"] in self.data["asked"] or not (delay <= time.time() - o["t"] <= delay + 2 * 86400):
                    continue
                chat = (self.app.state.data.get("open_orders", {}).get(f"{o['p']}:{o['id']}") or {}).get("chat_id")
                chat = chat or self._chat_of(o)
                bkey = f"{o['p']}:{str(o.get('buyer', '')).lower()}"
                if not chat or bkey in self.data["pending"] or \
                        time.time() - self.data.setdefault("buyers", {}).get(bkey, 0) < 86400:
                    self.data["asked"][o["id"]] = time.time()  # одного покупателя спрашиваем не чаще раза в сутки
                    continue
                self.data["buyers"][bkey] = time.time()
                self.data["asked"][o["id"]] = time.time()
                self.data["pending"][f"{o['p']}:{str(o.get('buyer', '')).lower()}"] = {
                    "order": o["id"], "chat": chat, "account": o.get("a"), "title": o.get("title"), "t": time.time()}
                self.reply(o["p"], chat, fmt_template(self.get("text_ask"), username=o.get("buyer"), order_title=o.get("title"),
                                                      order_id=o["id"]), o.get("a"), o.get("buyer"))
            for k, p in list(self.data["pending"].items()):
                if time.time() - p["t"] > 2 * 86400:
                    self.data["pending"].pop(k)
            self.save()

    def _chat_of(self, o):
        for e in reversed(list(self.app.events)):
            if e.get("kind") == "order" and str(e.get("id")) == str(o["id"]):
                return e.get("chat_id")
        return None

    def on_message(self, m) -> bool:
        key = f"{m.platform}:{str(m.author).lower()}"
        p = self.data["pending"].get(key)
        if not p or m.by_me or m.system:
            return False
        t = (m.text or "").strip()
        score = len(re.findall("⭐|★", t)) or (int(re.search(r"[1-5]", t).group(0)) if re.fullmatch(r"\D{0,6}[1-5]\D{0,10}", t) else 0)
        if not score:
            return False
        with self.lock:
            self.data["pending"].pop(key, None)
            self.data["scores"] = (self.data["scores"] + [score])[-500:]
            self.save()
        if score >= int(self.get("good_from", 4) or 4):
            self.reply(m.platform, m.chat_id, self.get("text_good"), m.account, m.chat_name)
        else:
            self.reply(m.platform, m.chat_id, self.get("text_bad"), m.account, m.chat_name)
            self.notify(f"📝 <b>Недоволен покупатель</b> {esc(m.author)}: оценка {score}/5 за «{esc(p.get('title') or '')}»\n"
                        f"Напишите ему, пока он не оставил плохой отзыв.",
                        buttons=[[("↩️ Ответить", f"duo:reply:{m.account}:{m.chat_id}", "chat")]], kind="errors")
        return True

    def status(self) -> str:
        sc = self.data["scores"]
        return (f"средняя оценка {sum(sc) / len(sc):.1f} из {len(sc)}" if sc else "оценок пока нет") + \
            (f" · ждут ответа: {len(self.data['pending'])}" if self.data["pending"] else "")
