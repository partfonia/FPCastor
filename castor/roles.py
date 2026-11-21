"""Роли и права: владелец, администратор, менеджер."""
from __future__ import annotations

import secrets
import time

# Права:
#   chat       — читать чаты, отвечать покупателям, шаблоны
#   orders     — видеть заказы (без денег)
#   customers  — база покупателей и заметки
#   products   — пополнять файлы товаров
#   delivery   — правила автовыдачи, ручная выдача, лоты, поставщики (без ключей)
#   finance    — суммы, прибыль, баланс, статистика, расходы
#   refund     — возвраты
#   settings   — настройки площадок, автоответы, функции, уведомления
#   plugins    — плагины
#   system     — бот и прокси, логи, бекапы, перезапуск
#   secrets    — ключи аккаунтов, токены, пароли, ключи поставщиков
#   withdraw   — вывод средств
#   users      — команда и роли
ALL_PERMS = ("chat", "orders", "customers", "products", "delivery", "finance", "refund", "settings",
             "plugins", "system", "secrets", "withdraw", "users")

ROLES = {
    "owner": set(ALL_PERMS),
    "admin": set(ALL_PERMS) - {"secrets", "withdraw", "users"},
    "manager": {"chat", "orders", "customers", "products"},
}
ROLE_NAMES = {"owner": "Владелец", "admin": "Администратор", "manager": "Менеджер"}
ROLE_HINTS = {
    "owner": "всё, включая ключи, вывод средств и команду",
    "admin": "все настройки, возвраты, финансы; без ключей, вывода и управления командой",
    "manager": "чаты и ответы покупателям, заказы без сумм, пополнение товаров, заметки",
}

# какое право нужно, чтобы получать уведомление данного типа
NOTIFY_PERM = {
    "fp_message": "chat", "pl_message": "chat", "reminder": "chat",
    "fp_order": "orders", "pl_deal": "orders", "fp_confirm": "orders", "pl_confirm": "orders",
    "fp_review": "orders", "pl_review": "orders", "delivery": "orders",
    "low_stock": "products", "stock_out": "delivery", "supplier": "delivery",
    "raise": "settings", "errors": "system", "start": "system", "report": "finance",
    "withdraw": "withdraw", "finance": "finance",
}


def perms_of(role: str) -> set:
    return set(ROLES.get(role, set()))


class Team:
    """Пользователи Telegram с ролями (config: telegram.users) и приглашения (state: invites)."""

    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state

    def users(self) -> list:
        return list(self.cfg.get("telegram.users", []))

    def role(self, uid) -> str:
        for u in self.users():
            if int(u.get("id", 0)) == int(uid):
                return u.get("role", "manager")
        return ""

    def can(self, uid, perm) -> bool:
        return perm in perms_of(self.role(uid)) if perm else bool(self.role(uid))

    def ids_with(self, perm) -> list:
        return [int(u["id"]) for u in self.users() if perm in perms_of(u.get("role", ""))]

    def owners(self) -> list:
        return [int(u["id"]) for u in self.users() if u.get("role") == "owner"]

    def add(self, uid, role="manager", name=""):
        users = [u for u in self.users() if int(u["id"]) != int(uid)]
        users.append({"id": int(uid), "role": role if role in ROLES else "manager", "name": name or ""})
        self.cfg.set("telegram.users", users)

    def set_role(self, uid, role) -> bool:
        users = self.users()
        if role not in ROLES:
            return False
        # нельзя оставить бота без владельца
        if role != "owner" and self.role(uid) == "owner" and len(self.owners()) <= 1:
            return False
        for u in users:
            if int(u["id"]) == int(uid):
                u["role"] = role
        self.cfg.set("telegram.users", users)
        return True

    def remove(self, uid) -> bool:
        if self.role(uid) == "owner" and len(self.owners()) <= 1:
            return False
        self.cfg.set("telegram.users", [u for u in self.users() if int(u["id"]) != int(uid)])
        return True

    # ---- приглашения ----
    def invite(self, role, created_by, ttl=86400) -> str:
        code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        self.state.mark("invites", code, {"role": role, "by": created_by, "exp": time.time() + ttl})
        return code

    def redeem(self, code, uid, name="") -> str:
        inv = self.state.get("invites", code)
        if not inv or inv.get("exp", 0) < time.time():
            self.state.pop("invites", code)
            return ""
        self.state.pop("invites", code)  # одноразовое
        if self.role(uid) == "owner":
            return "owner"
        self.add(uid, inv["role"], name)
        return inv["role"]
