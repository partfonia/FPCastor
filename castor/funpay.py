"""FunPay: клиент (golden_key) и типы, совместимые с FunPayAPI из FunPay Cardinal."""
from __future__ import annotations

import json
import random
import re
import string
import threading
import time
import types
from enum import Enum
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .core import DEFAULT_UA, proxies_dict
class MessageTypes(Enum):
    NON_SYSTEM = 0
    ORDER_PURCHASED = 1
    ORDER_CONFIRMED = 2
    NEW_FEEDBACK = 3
    FEEDBACK_CHANGED = 4
    FEEDBACK_DELETED = 5
    NEW_FEEDBACK_ANSWER = 6
    FEEDBACK_ANSWER_CHANGED = 7
    FEEDBACK_ANSWER_DELETED = 8
    ORDER_REOPENED = 9
    REFUND = 10
    PARTIAL_REFUND = 11
    ORDER_CONFIRMED_BY_ADMIN = 12
    DISCORD = 13
    DEAR_VENDORS = 14
    REFUND_BY_ADMIN = 15


class OrderStatuses(Enum):
    PAID = 0
    CLOSED = 1
    REFUNDED = 2


class SubCategoryTypes(Enum):
    COMMON = 0
    CURRENCY = 1


_SYS_PATTERNS = [
    (MessageTypes.ORDER_PURCHASED, r"оплатил заказ|paid for order|оплатив замовлення"),
    (MessageTypes.ORDER_CONFIRMED_BY_ADMIN, r"администратор .*подтвердил"),
    (MessageTypes.ORDER_CONFIRMED, r"подтвердил успешное выполнение|has confirmed that order|підтвердив"),
    (MessageTypes.NEW_FEEDBACK_ANSWER, r"ответил на отзыв|replied to their feedback"),
    (MessageTypes.FEEDBACK_ANSWER_CHANGED, r"изменил ответ на отзыв"),
    (MessageTypes.FEEDBACK_ANSWER_DELETED, r"удалил ответ на отзыв"),
    (MessageTypes.NEW_FEEDBACK, r"написал отзыв|has given feedback|написав відгук"),
    (MessageTypes.FEEDBACK_CHANGED, r"изменил отзыв|has edited their feedback"),
    (MessageTypes.FEEDBACK_DELETED, r"удалил отзыв|has deleted their feedback"),
    (MessageTypes.ORDER_REOPENED, r"открыт повторно|has been reopened"),
    (MessageTypes.REFUND_BY_ADMIN, r"администратор .*вернул"),
    (MessageTypes.PARTIAL_REFUND, r"часть средств"),
    (MessageTypes.REFUND, r"вернул деньги покупателю|refunded the buyer|повернув гроші"),
]


def detect_msg_type(text: str, author_id) -> MessageTypes:
    if str(author_id) != "0":
        return MessageTypes.NON_SYSTEM
    for t, pat in _SYS_PATTERNS:
        if re.search(pat, text or "", re.I):
            return t
    if "discord" in (text or "").lower():
        return MessageTypes.DISCORD
    return MessageTypes.DEAR_VENDORS


class FPMessage:
    """Совместим с FunPayAPI.types.Message (Cardinal)."""

    def __init__(self, id, text, chat_id, chat_name, author, author_id, html_="", by_bot=False, image_link=None):
        self.id, self.text, self.chat_id, self.chat_name = id, text, chat_id, chat_name
        self.author, self.author_id, self.html, self.by_bot = author, author_id, html_, by_bot
        self.image_link = image_link
        self.image_name = None
        self.badge = None
        self.interlocutor_id = None
        self.type = detect_msg_type(text, author_id)
        self.initiator_username = None
        self.initiator_id = None
        self.i_am_seller = None
        self.i_am_buyer = None

    def __str__(self):
        return self.text or (self.image_link or "")


class FPOrderShortcut:
    """Совместим с FunPayAPI.types.OrderShortcut."""

    def __init__(self, id, description, price, currency, buyer_username, buyer_id, status, date_text="",
                 subcategory_name="", html_="", amount=1, chat_id=None):
        self.id, self.description, self.price, self.currency = id, description, price, currency
        self.buyer_username, self.buyer_id, self.status = buyer_username, buyer_id, status
        self.date, self.subcategory_name, self.html = date_text, subcategory_name, html_
        self.amount, self.chat_id = amount, chat_id
        self.subcategory = None
        self.title = description

    def __str__(self):
        return self.description


class FPReview:
    def __init__(self, stars=None, text=None, reply=None, author=None, author_id=None, order_id=None):
        self.stars, self.text, self.reply = stars, text, reply
        self.author, self.author_id, self.order_id = author, author_id, order_id
        self.hidden = False


class FPOrder:
    """Совместим с FunPayAPI.types.Order (основные поля)."""

    def __init__(self, **kw):
        self.id = kw.get("id")
        self.status = kw.get("status", OrderStatuses.PAID)
        self.short_description = kw.get("short_description", "")
        self.full_description = kw.get("full_description", "")
        self.title = self.short_description
        self.sum = kw.get("sum", 0.0)
        self.price = self.sum
        self.currency = kw.get("currency", "₽")
        self.buyer_id = kw.get("buyer_id")
        self.buyer_username = kw.get("buyer_username")
        self.seller_id = kw.get("seller_id")
        self.seller_username = kw.get("seller_username")
        self.chat_id = kw.get("chat_id")
        self.amount = kw.get("amount", 1)
        self.subcategory = None
        self.subcategory_name = kw.get("subcategory_name", "")
        self.review = kw.get("review")
        self.params = kw.get("params", {})
        self.html = kw.get("html", "")
        self.lot_params = []


class FunPayError(Exception):
    pass


class FunPayClient:
    BASE = "https://funpay.com"

    def __init__(self, golden_key: str, user_agent: str = DEFAULT_UA, proxy: str = ""):
        self.golden_key = golden_key.strip()
        self.user_agent = user_agent or DEFAULT_UA
        self.s = requests.Session()
        self.s.headers.update({"user-agent": self.user_agent, "accept-language": "ru,en;q=0.9"})
        self.s.cookies.set("golden_key", self.golden_key, domain="funpay.com")
        self.s.cookies.set("cookie_prefs", "1", domain="funpay.com")
        if proxy:
            self.s.proxies.update(proxies_dict(proxy))
        self.id = None
        self.username = None
        self.csrf_token = None
        self.balance = None
        self.currency = "₽"
        self.locale = "ru"
        self.tag = self._tag()
        self.chat_names: dict = {}       # chat_id -> имя собеседника
        self.name_to_chat: dict = {}     # имя -> chat_id
        self.is_initiated = False
        self._lock = threading.RLock()

    @staticmethod
    def _tag():
        return "".join(random.choice(string.digits + string.ascii_lowercase) for _ in range(8))

    # ---- низкоуровневое ----
    def _req(self, method, path, retry=True, **kw):
        url = path if path.startswith("http") else self.BASE + path
        kw.setdefault("timeout", 20)
        if not retry:  # денежные операции: ровно одна попытка, без повторов
            try:
                return self.s.request(method, url, **kw)
            except requests.RequestException as e:
                raise FunPayError(f"Сеть: {e} — результат операции неизвестен, проверьте на сайте")
        for attempt in range(3):
            try:
                r = self.s.request(method, url, **kw)
                if r.status_code in (403, 401):
                    raise FunPayError(f"Доступ запрещён ({r.status_code}). Проверьте golden_key")
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                return r
            except requests.RequestException as e:
                if attempt == 2:
                    raise FunPayError(f"Сеть: {e}")
                time.sleep(2 * (attempt + 1))
        raise FunPayError(f"Не удалось выполнить запрос {path}")

    def _xhr_headers(self):
        return {"x-requested-with": "XMLHttpRequest",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "accept": "application/json, text/javascript, */*; q=0.01"}

    # ---- авторизация ----
    def login(self):
        r = self._req("GET", "/")
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("body")
        app_data = json.loads(body.get("data-app-data", "{}")) if body else {}
        uid = app_data.get("userId")
        if not uid:
            raise FunPayError("Не авторизован: неверный или просроченный golden_key")
        self.id = int(uid)
        self.csrf_token = app_data.get("csrf-token")
        self.locale = app_data.get("locale", "ru")
        name = soup.find("div", class_="user-link-name")
        self.username = name.text.strip() if name else str(self.id)
        bal = soup.find("span", class_="badge-balance")
        if bal:
            txt = bal.text.strip()
            self.balance = float(re.sub(r"[^\d.]", "", txt.replace(",", ".").replace(" ", "")) or 0)
            self.currency = re.sub(r"[\d.,\s]", "", txt) or "₽"
        else:
            self.balance = 0.0
        self.is_initiated = True
        return self

    get = login  # совместимость с FunPayAPI.Account.get()

    # ---- runner ----
    def runner(self, objects: list, request=None) -> dict:
        data = {"objects": json.dumps(objects), "request": json.dumps(request) if request else "false",
                "csrf_token": self.csrf_token}
        r = self._req("POST", "/runner/", data=data, headers=self._xhr_headers())
        try:
            return r.json()
        except Exception:
            raise FunPayError("runner вернул не JSON (возможно, слетела сессия)")

    def poll(self, last_counters_tag=None, last_bookmarks_tag=None) -> dict:
        objs = [
            {"type": "orders_counters", "id": self.id, "tag": last_counters_tag or self._tag(), "data": False},
            {"type": "chat_bookmarks", "id": self.id, "tag": last_bookmarks_tag or self._tag(), "data": False},
        ]
        return self.runner(objs)

    def parse_bookmarks(self, html_: str) -> list:
        soup = BeautifulSoup(html_ or "", "html.parser")
        chats = []
        for a in soup.find_all("a", class_="contact-item"):
            cid = a.get("data-id")
            name_el = a.find("div", class_="media-user-name")
            msg_el = a.find("div", class_="contact-item-message")
            name = name_el.text.strip() if name_el else ""
            chats.append({"id": int(cid) if str(cid).isdigit() else cid, "name": name,
                          "last_text": msg_el.text.strip() if msg_el else "",
                          "unread": "unread" in (a.get("class") or []),
                          "node_msg": int(a.get("data-node-msg") or 0),
                          "user_msg": int(a.get("data-user-msg") or 0)})
            self.chat_names[chats[-1]["id"]] = name
            self.name_to_chat[name] = chats[-1]["id"]
        return chats

    def get_chats(self, update=True) -> list:
        res = self.runner([{"type": "chat_bookmarks", "id": self.id, "tag": self._tag(), "data": False}])
        for o in res.get("objects", []):
            if o.get("type") == "chat_bookmarks":
                return self.parse_bookmarks(o.get("data", {}).get("html", ""))
        return []

    def get_chat_history(self, chat_id, chat_name=None, last_message=-1) -> list:
        obj = {"type": "chat_node", "id": chat_id, "tag": "00000000",
               "data": {"node": chat_id, "last_message": last_message, "content": ""}}
        res = self.runner([obj])
        msgs = []
        for o in res.get("objects", []):
            if o.get("type") != "chat_node" or not o.get("data"):
                continue
            data = o["data"]
            name = chat_name or self.chat_names.get(chat_id) or (data.get("node") or {}).get("name", "")
            last_author_name = None
            for m in data.get("messages", []):
                soup = BeautifulSoup(m.get("html", ""), "html.parser")
                a_el = soup.find(class_="chat-msg-author-link") or soup.find(class_="media-user-name")
                if a_el:
                    last_author_name = a_el.text.strip()
                author_id = m.get("author", 0)
                author = last_author_name or ("FunPay" if str(author_id) == "0" else "")
                if str(author_id) == str(self.id):
                    author = self.username
                t_el = soup.find(class_="chat-msg-text") or soup.find(class_="message-text")
                img = soup.find("a", class_="chat-img-link")
                text = t_el.get_text("\n").strip() if t_el else ""
                msgs.append(FPMessage(int(m.get("id", 0)), text, chat_id, name, author, author_id,
                                      m.get("html", ""), image_link=img.get("href") if img else None))
        return msgs

    def send_message(self, chat_id, text=None, chat_name=None, interlocutor_id=None, image_id=None,
                     add_to_ignore_list=True, update_last_saved_message=False, leave_as_unread=False, **kw):
        if chat_id is None and interlocutor_id:
            chat_id = self.private_node(interlocutor_id)
        obj = {"type": "chat_node", "id": chat_id, "tag": "00000000",
               "data": {"node": chat_id, "last_message": -1, "content": ""}}
        req = {"action": "chat_message", "data": {"node": chat_id, "last_message": -1, "content": text or ""}}
        if image_id:
            req["data"]["image_id"] = image_id
            req["data"]["content"] = ""
        with self._lock:
            res = self.runner([obj], req)
        resp = res.get("response") or {}
        if resp.get("error"):
            raise FunPayError(f"Ошибка отправки: {resp.get('error')}")
        return FPMessage(0, text, chat_id, chat_name, self.username, self.id, by_bot=True)

    def private_node(self, user_id) -> str:
        a, b = sorted([int(self.id), int(user_id)])
        return f"users-{a}-{b}"

    def chat_for(self, username=None, user_id=None):
        if username and username in self.name_to_chat:
            return self.name_to_chat[username]
        if username:
            try:
                self.get_chats()
                if username in self.name_to_chat:
                    return self.name_to_chat[username]
            except Exception:
                pass
        return self.private_node(user_id) if user_id else None

    def get_chat_by_name(self, name, make_request=False):
        cid = self.chat_for(name)
        return types.SimpleNamespace(id=cid, name=name) if cid else None

    # ---- продажи ----
    def get_sales(self, **kw) -> list:
        r = self._req("GET", "/orders/trade")
        soup = BeautifulSoup(r.text, "html.parser")
        orders = []
        for a in soup.find_all("a", class_="tc-item"):
            cls = a.get("class") or []
            status = OrderStatuses.CLOSED
            if "warning" in cls:
                status = OrderStatuses.REFUNDED
            elif "info" in cls:
                status = OrderStatuses.PAID
            oid_el = a.find("div", class_="tc-order")
            oid = oid_el.text.strip().lstrip("#") if oid_el else ""
            desc_el = a.find("div", class_="order-desc")
            desc = ""
            sub = ""
            if desc_el:
                divs = desc_el.find_all("div")
                desc = divs[0].text.strip() if divs else desc_el.text.strip()
                if len(divs) > 1:
                    sub = divs[-1].text.strip()
            price_el = a.find("div", class_="tc-price")
            price_txt = price_el.text.strip() if price_el else "0"
            try:
                price = float(re.sub(r"[^\d.]", "", price_txt.replace(",", ".").replace(" ", "")) or 0)
            except ValueError:
                price = 0.0
            cur = re.sub(r"[\d.,\s]", "", price_txt) or "₽"
            buyer_el = a.find("div", class_="media-user-name")
            buyer = buyer_el.text.strip() if buyer_el else ""
            bid = None
            span = a.find("span", class_="pseudo-a")
            if span and span.get("data-href"):
                m = re.search(r"/users/(\d+)", span["data-href"])
                bid = int(m.group(1)) if m else None
            date_el = a.find("div", class_="tc-date-time")
            am = re.search(r"(\d+)\s*(шт|pcs|ед)", desc, re.I)
            orders.append(FPOrderShortcut(oid, desc, price, cur, buyer, bid, status,
                                          date_el.text.strip() if date_el else "", sub, str(a),
                                          int(am.group(1)) if am else 1))
        return orders

    def get_order(self, order_id) -> FPOrder:
        r = self._req("GET", f"/orders/{order_id}/")
        soup = BeautifulSoup(r.text, "html.parser")
        params = {}
        for item in soup.find_all("div", class_="param-item"):
            h = item.find("h5")
            if not h:
                continue
            key = h.text.strip()
            h.extract()
            params[key] = item.get_text(" ", strip=True)

        def pget(*names):
            for n in names:
                for k, v in params.items():
                    if n.lower() in k.lower():
                        return v
            return ""
        short = pget("Краткое описание", "Short description")
        full = pget("Подробное описание", "Detailed description")
        sub = pget("Категория", "Category")
        amount_txt = pget("Количество", "Amount")
        sum_txt = pget("Сумма", "Total")
        status = OrderStatuses.PAID
        st = soup.find("span", class_=re.compile("text-(success|warning)"))
        if st:
            status = OrderStatuses.CLOSED if "success" in " ".join(st.get("class")) else OrderStatuses.REFUNDED
        chat = soup.find("div", class_="chat")
        chat_id = None
        if chat:
            chat_id = chat.get("data-id") or chat.get("data-name")
            chat_id = int(chat_id) if str(chat_id).isdigit() else chat_id
        buyer_id, buyer = None, None
        for a in soup.select(".param-item a[href*='/users/'], .chat-header a[href*='/users/']"):
            m = re.search(r"/users/(\d+)", a.get("href", ""))
            if m and int(m.group(1)) != self.id:
                buyer_id, buyer = int(m.group(1)), a.text.strip()
                break
        review = None
        rv = soup.find("div", class_="review-item")
        if rv:
            rating = rv.find("div", class_=re.compile(r"rating\d"))
            stars = None
            if rating:
                m = re.search(r"rating(\d)", " ".join(rating.get("class")))
                stars = int(m.group(1)) if m else None
            txt = rv.find("div", class_="review-item-text")
            reply = rv.find("div", class_="review-item-answer")
            review = FPReview(stars, txt.text.strip() if txt else "", reply.text.strip() if reply else None,
                              buyer, buyer_id, order_id)
        try:
            ssum = float(re.sub(r"[^\d.]", "", sum_txt.replace(",", ".").replace(" ", "")) or 0)
        except ValueError:
            ssum = 0.0
        return FPOrder(id=order_id, status=status, short_description=short, full_description=full,
                       subcategory_name=sub, amount=int(re.sub(r"\D", "", amount_txt) or 1), sum=ssum,
                       buyer_id=buyer_id, buyer_username=buyer, seller_id=self.id,
                       seller_username=self.username, chat_id=chat_id, review=review, params=params,
                       html=r.text)

    def refund(self, order_id):
        r = self._req("POST", "/orders/refund", retry=False, data={"id": order_id, "csrf_token": self.csrf_token},
                      headers=self._xhr_headers())
        j = r.json() if r.text.strip().startswith("{") else {}
        if j.get("error"):
            raise FunPayError(j.get("msg") or "Ошибка возврата")
        return True

    def send_review(self, order_id, text, rating=5):
        r = self._req("POST", "/orders/review",
                      data={"authorId": self.id, "text": text, "rating": "", "csrf_token": self.csrf_token,
                            "orderId": order_id}, headers=self._xhr_headers())
        j = r.json() if r.text.strip().startswith("{") else {}
        if j.get("error"):
            raise FunPayError(j.get("msg") or "Ошибка ответа на отзыв")
        return True

    # ---- лоты / поднятие ----
    def get_my_lot_nodes(self) -> list:
        r = self._req("GET", f"/users/{self.id}/")
        soup = BeautifulSoup(r.text, "html.parser")
        nodes = []
        for a in soup.select("div.offer-list-title a[href]"):
            m = re.search(r"/lots/(\d+)/", a["href"])
            if m:
                nodes.append({"node": int(m.group(1)), "name": a.text.strip()})
        return nodes

    def get_game_id(self, node_id) -> Optional[int]:
        r = self._req("GET", f"/lots/{node_id}/trade")
        soup = BeautifulSoup(r.text, "html.parser")
        btn = soup.find(class_="js-lot-raise")
        if btn and btn.get("data-game"):
            return int(btn["data-game"])
        m = re.search(r'data-game="(\d+)"', r.text)
        return int(m.group(1)) if m else None

    def raise_lots(self, game_id, node_ids: list) -> dict:
        data = {"game_id": game_id, "node_id": node_ids[0], "node_ids[]": node_ids}
        r = self._req("POST", "/lots/raise", data=data, headers=self._xhr_headers())
        try:
            return r.json()
        except Exception:
            return {"error": True, "msg": r.text[:200]}

    def get_user(self, user_id):
        r = self._req("GET", f"/users/{user_id}/")
        soup = BeautifulSoup(r.text, "html.parser")
        n = soup.find("span", class_="mr4") or soup.find("div", class_="media-user-name")
        return types.SimpleNamespace(id=user_id, username=n.text.strip() if n else str(user_id), html=r.text)

    def set_proxy(self, proxy: str):
        self.s.proxies.clear()
        if proxy:
            self.s.proxies.update(proxies_dict(proxy))

    # ═════════ свои лоты: список, включение/выключение ═════════
    def get_my_lots(self, extra_nodes=()) -> list:
        """Все свои лоты (включая неактивные) по разделам, где они есть.
        Активность: у неактивного лота в списке раздела есть класс warning."""
        nodes = {n["node"]: n["name"] for n in self.get_my_lot_nodes()}
        for n in extra_nodes:
            nodes.setdefault(int(n), "")
        lots = []
        for node, node_name in nodes.items():
            try:
                r = self._req("GET", f"/lots/{node}/trade")
            except FunPayError:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            if not node_name:
                h = soup.find("h1")
                node_name = h.text.strip() if h else str(node)
            for a in soup.find_all("a", class_="tc-item"):
                oid = a.get("data-offer")
                if not oid:
                    m = re.search(r"offer(?:Edit)?\?(?:id|offer)=(\d+)", a.get("href", ""))
                    oid = m.group(1) if m else None
                if not oid:
                    continue
                desc = a.find(class_="tc-desc-text")
                price = a.find(class_="tc-price")
                amount = a.find(class_="tc-amount")
                lots.append({"id": str(oid), "node": node, "node_name": node_name,
                             "title": desc.text.strip() if desc else "",
                             "price": (price.get("data-s") or price.text.strip()) if price else "",
                             "amount": amount.text.strip() if amount else "",
                             "active": "warning" not in (a.get("class") or [])})
            time.sleep(0.4)
        return lots

    def get_lot_form(self, lot_id) -> list:
        """Поля формы редактирования лота — в точности как отправил бы браузер (список пар)."""
        r = self._req("GET", f"/lots/offerEdit?offer={lot_id}")
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form", class_=re.compile("form-offer-editor")) or \
            next((f for f in soup.find_all("form") if "offerSave" in (f.get("action") or "")), None)
        if not form:
            raise FunPayError("Форма редактирования лота не найдена (лот удалён или нет доступа)")
        fields = []
        for el in form.find_all(["input", "textarea", "select"]):
            name = el.get("name")
            if not name or el.has_attr("disabled"):
                continue
            if el.name == "input":
                t = (el.get("type") or "text").lower()
                if t in ("submit", "button", "file", "image", "reset"):
                    continue
                if t in ("checkbox", "radio"):
                    if el.has_attr("checked"):
                        fields.append((name, el.get("value", "on")))
                    continue
                fields.append((name, el.get("value", "")))
            elif el.name == "textarea":
                fields.append((name, el.text))
            else:
                opt = el.find("option", selected=True) or el.find("option")
                if opt is not None:
                    fields.append((name, opt.get("value", opt.text)))
        return fields

    def lot_is_active(self, lot_id) -> bool:
        return any(k == "active" for k, _ in self.get_lot_form(lot_id))

    def set_lot_active(self, lot_id, active: bool) -> bool:
        """Включает/выключает лот и проверяет результат повторным чтением формы."""
        fields = [(k, v) for k, v in self.get_lot_form(lot_id) if k != "active"]
        if active:
            fields.append(("active", "on"))
        if not any(k == "offer_id" for k, _ in fields):
            fields.append(("offer_id", str(lot_id)))
        if not any(k == "csrf_token" for k, _ in fields):
            fields.append(("csrf_token", self.csrf_token))
        r = self._req("POST", "/lots/offerSave", data=fields, headers=self._xhr_headers())
        try:
            j = r.json()
        except Exception:
            j = {}
        if j.get("error") or j.get("errors"):
            raise FunPayError(f"FunPay не сохранил лот: {j.get('error') or j.get('errors')}")
        time.sleep(0.6)
        if self.lot_is_active(lot_id) != active:
            raise FunPayError("FunPay принял запрос, но состояние лота не изменилось")
        return True

    # ═════════ баланс и вывод ═════════
    def get_available_balance(self, currency="rub") -> Optional[float]:
        """Доступный к выводу баланс со страницы /account/balance (если не удалось — None)."""
        try:
            r = self._req("GET", "/account/balance")
        except FunPayError:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        sym = {"rub": "₽", "usd": "$", "eur": "€"}.get(currency, "₽")
        for el in soup.select(".balances-value, .balance-item, .balances-list span, .profile-balance-value"):
            txt = el.get_text(" ", strip=True)
            if sym in txt:
                num = re.sub(r"[^\d.]", "", txt.replace(",", ".").replace("\xa0", "").replace(" ", ""))
                try:
                    return float(num)
                except ValueError:
                    continue
        return None

    def withdraw(self, currency: str, method: str, wallet: str, amount: float) -> dict:
        """Заявка на вывод. Формат запроса — как в открытом FunPayAPI (withdraw/withdraw).
        Возвращает ответ FunPay; при ошибке — FunPayError (повторять автоматически нельзя)."""
        payload = {"csrf_token": self.csrf_token, "currency_id": currency, "ext_currency_id": method,
                   "wallet": wallet, "amount_int": str(int(amount))}
        r = self._req("POST", "/withdraw/withdraw", retry=False, data=payload, headers=self._xhr_headers())
        try:
            j = r.json()
        except Exception:
            raise FunPayError(f"Неожиданный ответ FunPay на вывод (HTTP {r.status_code})")
        if j.get("error"):
            raise FunPayError(j.get("msg") or "FunPay отклонил вывод")
        return j
