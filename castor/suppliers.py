"""API поставщиков: универсальный HTTP-коннектор для автовыдачи.

Поставщик описывается в конфиге (suppliers):
  id, name, enabled
  url, method (GET/POST), headers (строки «Имя: значение»), body (шаблон JSON или form)
  body_type: json | form
  items_path   — где в ответе товар(ы): «data.codes», «result.0.key», «items[*].code»
  cost_path    — где цена закупки (необязательно), cost_mult — множитель (например 0.01 для копеек)
  success_path / success_value — проверка успеха (необязательно)
  error_path   — где текст ошибки (необязательно)
  balance_url, balance_method, balance_path — проверка баланса (необязательно)
  timeout

Переменные в url/body/headers: $product_id $quantity $order_id $buyer $platform $title $account
Защита от двойной покупки: заказ сначала помечается «в процессе». Если бот упал или связь
оборвалась посреди запроса — повторно покупать он НЕ будет, а сообщит вам, чтобы проверить у поставщика.
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Optional

import requests

from .core import esc, log, proxies_dict

if TYPE_CHECKING:
    from .app import App


class SupplierError(Exception):
    pass


def get_path(data: Any, path: str):
    """«a.b.0.c», «items[*].code» (собрать из всех элементов списка)."""
    if not path:
        return data
    parts = re.findall(r"[^.\[\]]+|\[\*\]", path)
    cur = [data]
    for p in parts:
        nxt = []
        for c in cur:
            if p == "[*]" or p == "*":
                if isinstance(c, list):
                    nxt.extend(c)
            elif isinstance(c, dict):
                if p in c:
                    nxt.append(c[p])
            elif isinstance(c, list) and p.lstrip("-").isdigit():
                i = int(p)
                if -len(c) <= i < len(c):
                    nxt.append(c[i])
        cur = nxt
        if not cur:
            return None
    return cur if "[*]" in path or "*" in parts else cur[0]


def _subst(text: str, v: dict, json_mode=False) -> str:
    out = str(text or "")
    for k in sorted(v, key=len, reverse=True):
        val = v[k]
        if json_mode and isinstance(val, str):
            val = json.dumps(val, ensure_ascii=False)[1:-1]  # экранируем для вставки внутрь JSON-строки
        out = out.replace("$" + k, str(val))
    return out


def _headers(raw: str, v: dict) -> dict:
    h = {}
    for line in str(raw or "").splitlines():
        if ":" in line:
            k, val = line.split(":", 1)
            h[k.strip()] = _subst(val.strip(), v)
    return h


class Suppliers:
    def __init__(self, app: "App"):
        self.app = app

    def get(self, sid) -> Optional[dict]:
        return next((s for s in self.app.cfg.get("suppliers", []) if s.get("id") == sid), None)

    def build_request(self, sup: dict, v: dict) -> dict:
        method = (sup.get("method") or "POST").upper()
        url = _subst(sup.get("url", ""), {k: requests.utils.quote(str(x)) for k, x in v.items()})
        req = {"method": method, "url": url, "headers": _headers(sup.get("headers", ""), v),
               "timeout": float(sup.get("timeout") or 30)}
        body = sup.get("body") or ""
        if method != "GET" and body:
            if (sup.get("body_type") or "json") == "json":
                txt = _subst(body, v, json_mode=True)
                try:
                    req["json"] = json.loads(txt)
                except ValueError as e:
                    raise SupplierError(f"Шаблон тела запроса — неверный JSON: {e}")
            else:
                req["data"] = dict(x.split("=", 1) for x in _subst(body, v).split("&") if "=" in x)
        return req

    def _call(self, sup: dict, req: dict):
        proxy = sup.get("proxy") or ""
        try:
            r = requests.request(proxies=proxies_dict(proxy), **req)
        except requests.RequestException as e:
            raise SupplierError(f"нет ответа от поставщика: {e}")
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data

    def _check(self, sup, code, data):
        if code >= 400:
            msg = get_path(data, sup.get("error_path", "")) if sup.get("error_path") and isinstance(data, (dict, list)) else None
            raise SupplierError(f"HTTP {code}: {str(msg or data)[:300]}")
        if sup.get("success_path"):
            val = get_path(data, sup["success_path"]) if isinstance(data, (dict, list)) else None
            if str(val).lower() != str(sup.get("success_value", "true")).lower():
                msg = get_path(data, sup.get("error_path", "")) if sup.get("error_path") else None
                raise SupplierError(f"поставщик вернул ошибку: {str(msg or data)[:300]}")

    def extract(self, sup, data) -> tuple:
        raw = get_path(data, sup.get("items_path", "")) if isinstance(data, (dict, list)) else data
        if raw is None:
            raise SupplierError(f"в ответе нет товара по пути «{sup.get('items_path')}»: {str(data)[:300]}")
        if isinstance(raw, list):
            items = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in raw]
        elif isinstance(raw, (dict,)):
            items = [json.dumps(raw, ensure_ascii=False)]
        else:
            items = [x for x in str(raw).splitlines() if x.strip()] if sup.get("split_lines") else [str(raw)]
        cost = None
        if sup.get("cost_path") and isinstance(data, (dict, list)):
            c = get_path(data, sup["cost_path"])
            try:
                cost = round(float(c) * float(sup.get("cost_mult") or 1), 2)
            except (TypeError, ValueError):
                cost = None
        return items, cost

    def buy(self, sup_id: str, product_id: str, quantity: int, order) -> tuple:
        """Покупка у поставщика с защитой от повторной покупки. → (items, cost)"""
        sup = self.get(sup_id)
        if not sup or not sup.get("enabled", True):
            raise SupplierError(f"поставщик «{sup_id}» не найден или выключен")
        key = f"{order.account}:{order.id}:{sup_id}"
        prev = self.app.state.get("supplier_orders", key)
        if prev and prev.get("status") == "done":
            return prev["items"], prev.get("cost")
        if prev and prev.get("status") == "pending":
            raise SupplierError("заказ уже отправлялся поставщику, но ответ не дошёл — проверьте у поставщика вручную, "
                                "повторно не покупаю")
        v = {"product_id": product_id, "quantity": quantity, "order_id": order.id, "buyer": order.buyer,
             "platform": order.platform, "title": order.title, "account": order.account or ""}
        req = self.build_request(sup, v)
        self.app.state.mark("supplier_orders", key, {"status": "pending", "t": time.time(), "supplier": sup_id})
        try:
            code, data = self._call(sup, req)
        except SupplierError:
            raise  # статус pending остаётся: результат неизвестен
        try:
            self._check(sup, code, data)
            items, cost = self.extract(sup, data)
        except SupplierError:
            # поставщик явно ответил ошибкой — покупки не было, можно повторить
            self.app.state.pop("supplier_orders", key)
            raise
        self.app.state.mark("supplier_orders", key, {"status": "done", "t": time.time(), "items": items,
                                                     "cost": cost, "supplier": sup_id})
        log.info("🏭 Поставщик %s: заказ %s, получено %s шт.", sup.get("name") or sup_id, order.id, len(items))
        return items, cost

    def balance(self, sup_id) -> str:
        sup = self.get(sup_id)
        if not sup or not sup.get("balance_url"):
            raise SupplierError("у поставщика не указан адрес проверки баланса")
        req = {"method": (sup.get("balance_method") or "GET").upper(), "url": sup["balance_url"],
               "headers": _headers(sup.get("headers", ""), {}), "timeout": float(sup.get("timeout") or 30)}
        code, data = self._call(sup, req)
        if code >= 400:
            raise SupplierError(f"HTTP {code}: {str(data)[:200]}")
        val = get_path(data, sup.get("balance_path", "")) if isinstance(data, (dict, list)) else data
        return str(val)

    def preview(self, sup_id, product_id="TEST", quantity=1) -> dict:
        """Показывает запрос, который будет отправлен, НЕ отправляя его. Секреты в заголовках скрыты."""
        sup = self.get(sup_id)
        if not sup:
            raise SupplierError("поставщик не найден")
        req = self.build_request(sup, {"product_id": product_id, "quantity": quantity, "order_id": "ABC123",
                                       "buyer": "buyer", "platform": "funpay", "title": "Пример", "account": "fp1"})
        req["headers"] = {k: (v[:4] + "…" if len(v) > 8 else "***") for k, v in req["headers"].items()}
        return req

    def pending(self) -> list:
        return [dict(v, key=k) for k, v in self.app.state.data.get("supplier_orders", {}).items()
                if v.get("status") == "pending"]

    def resolve_pending(self, key):
        self.app.state.pop("supplier_orders", key)

    @staticmethod
    def describe_error(e: Exception) -> str:
        return esc(str(e))[:400]
