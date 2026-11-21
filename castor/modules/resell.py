"""Перепродажа через сервисы: SMM-панели (стандартный API v2) и LZT Market.

Оба модуля тратят ваши деньги на балансе сервиса — сначала проверьте на одной недорогой покупке.
"""
from __future__ import annotations

import re
import threading
import time

import requests

from ..core import esc, fmt_template, log, proxies_dict
from . import Module, order_from_dict, order_to_dict


def _match(rules, title):
    t = (title or "").lower()
    for i, r in enumerate(rules):
        kws = [k.strip().lower() for k in str(r.get("keyword", "")).split("|") if k.strip()]
        if r.get("enabled", True) is not False and kws and any(k in t for k in kws):
            return i, r
    return None, None


# ═════════════════════ SMM-панель ═════════════════════
class SmmModule(Module):
    KEY = "smm"
    GROUP = "Перепродажа"
    ORDER = 25
    NAME = "Накрутка через SMM-панель"
    ICON = "📈"
    DESCRIPTION = ("Продаёте подписчиков, лайки, просмотры? Бот спросит у покупателя ссылку, сам закажет услугу в вашей "
                   "SMM-панели (стандартный API v2), проследит за выполнением и напишет покупателю, когда готово.")
    DEFAULTS = {"api_url": "", "api_key": "", "rate": 1, "check_min": 5, "rules": [],
                "text_ask": "Пришлите ссылку, куда накрутить (профиль, пост или видео) 🔗",
                "text_bad": "Это не похоже на ссылку. Пришлите ссылку целиком, начиная с https://",
                "text_started": "Заказ запущен ✅ Номер: $smm_id. Сообщу, когда будет готово.",
                "text_done": "Готово! Заказ выполнен ✅ Подтвердите, пожалуйста, получение 🙏",
                "text_fail": "Сервис не смог выполнить заказ — продавец уже разбирается и скоро ответит."}
    FIELDS = [("api_url", "Адрес API панели", "text", "обычно https://сайт-панели/api/v2"),
              ("api_key", "API-ключ панели", "secret"),
              ("rules", "Услуги", "items"),
              ("rate", "Курс валюты панели к рублю", "number", "для учёта себестоимости: 1 — если панель в рублях, ~90 — если в $"),
              ("check_min", "Проверять статус каждые, мин", "number"),
              ("text_ask", "Вопрос о ссылке", "area"), ("text_bad", "Если ссылка неверная", "area"),
              ("text_started", "Заказ запущен", "area", "$smm_id — номер в панели"),
              ("text_done", "Заказ выполнен", "area"), ("text_fail", "Не получилось", "area")]
    ITEM_FIELDS = {"rules": [("keyword", "Слово в названии заказа", "text", "например: подписчики телеграм"),
                             ("service", "ID услуги в панели", "text", "из списка услуг панели"),
                             ("per_unit", "Сколько заказать за 1 шт. в заказе", "number", "например 1000 — если лот «1000 подписчиков»"),
                             ("link_regex", "Проверка ссылки (regex)", "text", r"например t\.me/|telegram\.me/ — пусто: любая https-ссылка"),
                             ("enabled", "Включено", "bool")]}

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("pending", {})   # ждём ссылку: "platform:buyer" → заказ
        self.data.setdefault("active", {})    # smm_id → заказ
        self.data.setdefault("done", 0)
        self._last = 0

    def api(self, **params) -> dict:
        url, key = str(self.get("api_url") or "").strip(), str(self.get("api_key") or "").strip()
        if not url or not key:
            raise RuntimeError("не указан адрес или ключ API панели")
        r = requests.post(url, data=dict(params, key=key), timeout=30)
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"панель ответила не JSON (HTTP {r.status_code})")
        if isinstance(j, dict) and j.get("error"):
            raise RuntimeError(str(j["error"]))
        return j

    def on_order(self, o) -> bool:
        i, r = _match(self.get("rules", []), o.title)
        if r is None:
            return False
        with self.lock:
            self.data["pending"][f"{o.platform}:{str(o.buyer).lower()}"] = dict(order_to_dict(o), rule=i, t=time.time())
            self.save()
        o.reply(self.get("text_ask"))
        return True

    def on_message(self, m) -> bool:
        key = f"{m.platform}:{str(m.author).lower()}"
        p = self.data["pending"].get(key)
        if not p or m.system or m.by_me:
            return False
        rules = self.get("rules", [])
        r = rules[p["rule"]] if p["rule"] < len(rules) else {}
        link = (re.search(r"https?://\S+", m.text or "") or re.search(r"@[\w.]{3,}", m.text or ""))
        rx = r.get("link_regex")
        if not link or (rx and not re.search(rx, link.group(0))):
            self.reply(m.platform, m.chat_id, self.get("text_bad"), m.account, m.chat_name)
            return True
        link = link.group(0)
        qty = int(float(r.get("per_unit") or 1) * int(p.get("amount") or 1))
        with self.lock:
            self.data["pending"].pop(key, None)
            self.save()
        try:
            res = self.api(action="add", service=str(r.get("service")), link=link, quantity=qty)
            sid = str(res.get("order"))
        except Exception as e:
            self.reply(m.platform, m.chat_id, self.get("text_fail"), m.account, m.chat_name)
            self.notify(f"📈 <b>SMM-панель не приняла заказ</b> <code>{esc(p['id'])}</code>: {esc(e)}\n"
                        f"Ссылка: {esc(link)} · услуга {esc(r.get('service'))} × {qty}", kind="supplier")
            return True
        with self.lock:
            self.data["active"][sid] = dict(p, link=link, qty=qty, started=time.time())
            self.save()
        self.reply(m.platform, m.chat_id, fmt_template(self.get("text_started"), smm_id=sid), m.account, m.chat_name)
        self.notify(f"📈 Накрутка запущена: заказ <code>{esc(p['id'])}</code> → панель #{esc(sid)} ({qty} шт.)")
        return True

    def tick(self):
        if not self.data["active"] or time.time() - self._last < max(1.0, float(self.get("check_min", 5) or 5)) * 60:
            return
        self._last = time.time()
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        for sid, p in list(self.data["active"].items()):
            try:
                st = self.api(action="status", order=sid)
            except Exception as e:
                log.warning("📈 Статус %s: %s", sid, e)
                continue
            status = str(st.get("status", "")).lower()
            o = order_from_dict(self.app, p)
            charge = st.get("charge")
            if status in ("completed", "partial", "canceled", "cancelled", "refunded"):
                with self.lock:
                    self.data["active"].pop(sid, None)
                    self.save()
                if charge not in (None, ""):
                    try:
                        self.app.stats.add_cost(p["id"], round(float(charge) * float(self.get("rate") or 1), 2), p["platform"])
                    except Exception:
                        pass
            if status == "completed":
                self.data["done"] += 1
                self.save()
                o.reply(self.get("text_done"))
                self.app.stats.set_order_flag(p["id"], "delivered", True, p["platform"])
                self.notify(f"📈 Накрутка выполнена: заказ <code>{esc(p['id'])}</code>")
            elif status == "partial":
                o.reply(self.get("text_fail"))
                self.notify(f"📈 <b>Выполнено частично</b>: заказ <code>{esc(p['id'])}</code>, не хватило {esc(st.get('remains'))}. "
                            f"Решите с покупателем: доплата, повтор или частичный возврат.", kind="supplier")
            elif status in ("canceled", "cancelled", "refunded"):
                o.reply(self.get("text_fail"))
                self.notify(f"📈 <b>Панель отменила заказ</b> <code>{esc(p['id'])}</code> (#{esc(sid)})", kind="supplier")
            time.sleep(1)

    def status(self) -> str:
        return f"выполнено: {self.data['done']} · в работе: {len(self.data['active'])} · ждут ссылку: {len(self.data['pending'])}"

    def actions(self) -> list:
        return [("balance", "💰 Баланс панели"), ("services", "📋 Найти ID услуги")]

    def run_action(self, action, args) -> str:
        try:
            if action == "balance":
                j = self.api(action="balance")
                return f"Баланс панели: {j.get('balance')} {j.get('currency', '')}"
            if action == "services":
                j = self.api(action="services")
                lines = [f"{s.get('service')}: {s.get('name')} — {s.get('rate')} за 1000" for s in (j or [])[:60]]
                return "Первые 60 услуг (ID: название — цена):\n" + "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"
        return "Нет такого действия"


# ═════════════════════ LZT Market ═════════════════════
class LztModule(Module):
    KEY = "lzt"
    GROUP = "Перепродажа"
    ORDER = 26
    NAME = "Аккаунты с LZT Market"
    ICON = "🛒"
    DESCRIPTION = ("Покупатель оплатил лот — бот сам находит самый дешёвый подходящий аккаунт на LZT Market (Steam, TikTok, "
                   "Telegram, ИИ-сервисы и другие разделы), покупает его и отправляет данные покупателю.")
    DEFAULTS = {"token": "", "api_url": "https://prod-api.lzt.market", "rules": [],
                "text": "Ваш аккаунт ✅\n$login_data\n\nСразу смените пароль и почту, если это возможно.",
                "text_wait": "Подбираю для вас аккаунт, это займёт пару минут ⏳",
                "text_fail": "Не получилось подобрать аккаунт автоматически — продавец уже в курсе и скоро ответит."}
    FIELDS = [("token", "Токен LZT Market (scope market)", "secret", "создаётся в настройках API на lzt.market"),
              ("rules", "Что покупать", "items"),
              ("text", "Сообщение с аккаунтом", "area", "$login_data — логин и пароль, $title — название"),
              ("text_wait", "Пока ищу", "area"), ("text_fail", "Если не получилось", "area")]
    ITEM_FIELDS = {"rules": [("keyword", "Слово в названии заказа", "text"),
                             ("category", "Раздел LZT", "text", "steam, telegram, tiktok, chatgpt, discord, fortnite, epicgames…"),
                             ("query", "Фильтры поиска (как в адресе LZT)", "text", "например: title=cs2&origin[]=autoreg"),
                             ("max_price", "Не дороже, ₽", "number"),
                             ("enabled", "Включено", "bool")]}

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("bought", 0)
        self.data.setdefault("spent", 0.0)

    def _req(self, method, path, **kw):
        token = str(self.get("token") or "").strip()
        if not token:
            raise RuntimeError("не указан токен LZT Market")
        url = str(self.get("api_url") or "https://prod-api.lzt.market").rstrip("/") + "/" + path.lstrip("/")
        for attempt in range(6):
            r = requests.request(method, url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                 timeout=40, **kw)
            try:
                j = r.json()
            except Exception:
                raise RuntimeError(f"LZT ответил HTTP {r.status_code}")
            errs = j.get("errors") or ([j["error"]] if j.get("error") else [])
            if any("retry_request" in str(e) for e in errs) or r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))   # LZT просит повторить запрос
                continue
            if errs:
                raise RuntimeError("; ".join(map(str, errs)))
            return j
        raise RuntimeError("LZT просит повторить запрос слишком много раз — попробуйте позже")

    @staticmethod
    def _login_data(item: dict) -> str:
        ld = item.get("loginData") or item.get("login_data") or {}
        if isinstance(ld, dict):
            if ld.get("raw"):
                return str(ld["raw"])
            if ld.get("login"):
                return f"Логин: {ld.get('login')}\nПароль: {ld.get('password', '')}"
        parts = []
        for k, name in (("login", "Логин"), ("password", "Пароль"), ("email", "Почта")):
            if item.get(k):
                parts.append(f"{name}: {item[k]}")
        em = item.get("emailLoginData") or item.get("email_login_data")
        if isinstance(em, dict) and em.get("raw"):
            parts.append(f"Почта: {em['raw']}")
        return "\n".join(parts)

    def on_order(self, o) -> bool:
        i, r = _match(self.get("rules", []), o.title)
        if r is None:
            return False
        o.reply(self.get("text_wait"))
        threading.Thread(target=self._buy, args=(order_to_dict(o), r), daemon=True).start()
        return True

    def _buy(self, od, r):
        o = order_from_dict(self.app, od)
        got, errors = [], []
        need = int(od.get("amount") or 1)
        try:
            q = str(r.get("query") or "").lstrip("?")
            maxp = float(r.get("max_price") or 0)
            path = f"/{str(r.get('category') or 'steam').strip('/')}?order_by=price_to_up" + (f"&pmax={maxp:g}" if maxp else "") + \
                (f"&{q}" if q else "")
            items = (self._req("GET", path).get("items") or [])[:need + 6]
            for it in items:
                if len(got) >= need:
                    break
                try:
                    res = self._req("POST", f"/{it['item_id']}/fast-buy", data={"price": it.get("price")})
                    item = res.get("item") or res
                    data = self._login_data(item)
                    if not data:
                        errors.append(f"#{it['item_id']}: куплен, но нет данных входа в ответе — проверьте на LZT")
                        continue
                    got.append((it, data))
                except Exception as e:
                    errors.append(f"#{it.get('item_id')}: {e}")
                time.sleep(1)
        except Exception as e:
            errors.append(str(e))
        if len(got) < need:
            o.reply(self.get("text_fail"))
            self.notify(f"🛒 <b>LZT: не удалось купить</b> для заказа <code>{esc(od['id'])}</code> "
                        f"({len(got)} из {need})\n" + "\n".join(f"• {esc(x)[:150]}" for x in errors[:6]), kind="supplier")
        for it, data in got:
            o.reply(fmt_template(self.get("text"), login_data=data, title=it.get("title", "")))
        if got:
            cost = sum(float(it.get("price") or 0) for it, _ in got)
            self.data["bought"] += len(got)
            self.data["spent"] = round(self.data["spent"] + cost, 2)
            self.save()
            self.app.stats.add_cost(od["id"], cost, od["platform"])
            if len(got) >= need:
                self.app.stats.set_order_flag(od["id"], "delivered", True, od["platform"])
            self.notify(f"🛒 LZT: куплено {len(got)} шт. за {cost:g} ₽ для заказа <code>{esc(od['id'])}</code>")

    def status(self) -> str:
        return f"куплено: {self.data['bought']} на {self.data['spent']:g} ₽"

    def actions(self) -> list:
        return [("me", "🔑 Проверить токен и баланс")]

    def run_action(self, action, args) -> str:
        if action == "me":
            try:
                j = self._req("GET", "/me")
                u = j.get("user") or j
                return f"Токен рабочий: {u.get('username', '?')}, баланс {u.get('balance', '?')} {u.get('currency', '')}"
            except Exception as e:
                return f"Ошибка: {e}"
        return "Нет такого действия"
