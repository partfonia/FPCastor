"""Автовывод средств с FunPay и Playerok.

Защита:
  • по умолчанию выключен; режим ask — бот спрашивает владельца, auto — выводит сам;
  • если доступный баланс узнать точно нельзя — бот не выводит сам, а спрашивает;
  • лимит в сутки, минимальная сумма, остаток на балансе;
  • одна попытка на заявку — никаких автоповторов (иначе возможен двойной вывод);
  • после любой ошибки автовывод ставится на паузу до решения владельца;
  • подтверждать может только пользователь с правом withdraw; кнопка одноразовая и живёт 6 часов.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .core import _g, esc, log

if TYPE_CHECKING:
    from .app import App


def mask_wallet(w: str) -> str:
    w = str(w or "")
    return ("•••• " + w[-4:]) if len(w) > 4 else "••••"


class WithdrawManager:
    def __init__(self, app: "App"):
        self.app = app
        self.lock = threading.Lock()

    def cfg(self, platform) -> dict:
        return self.app.cfg.get(f"withdraw.{platform}", {})

    # ---------- журнал ----------
    def log_entry(self, platform, account, amount, status, info=""):
        key = f"{int(time.time() * 1000)}"
        self.app.state.mark("withdraw_log", key, {"platform": platform, "account": account, "amount": amount,
                                                  "status": status, "info": str(info)[:300], "t": time.time()})
        return key

    def history(self, limit=50) -> list:
        items = sorted(self.app.state.data.get("withdraw_log", {}).values(), key=lambda x: -x["t"])
        return items[:limit]

    def withdrawn_today(self, platform) -> float:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return round(sum(x["amount"] for x in self.app.state.data.get("withdraw_log", {}).values()
                         if x["platform"] == platform and x["t"] >= start and x["status"] in ("done", "started")), 2)

    def paused(self, platform) -> Optional[str]:
        return self.app.state.get("flags", f"wd_paused:{platform}")

    def unpause(self, platform):
        self.app.state.pop("flags", f"wd_paused:{platform}")

    # ---------- баланс ----------
    def _service(self, platform):
        w = self.cfg(platform)
        pool = self.app.fp if platform == "funpay" else self.app.pl
        if w.get("account") and w["account"] in pool:
            svc = pool[w["account"]]
            return svc if svc.ok else None
        return next((s for s in pool.values() if s.ok), None)

    def available(self, platform) -> tuple:
        """→ (сумма или None, точно ли, id аккаунта)"""
        svc = self._service(platform)
        if not svc:
            return None, False, None
        if platform == "funpay":
            cur = self.cfg(platform).get("currency", "rub")
            v = svc.client.get_available_balance(cur)
            if v is not None:
                return v, True, svc.acc_id
            try:
                svc.client.login()
            except Exception:
                pass
            return svc.client.balance, False, svc.acc_id
        acc = svc.bridge.acc
        for path in (("profile", "balance", "withdrawable"), ("profile", "balance", "available"), ("balance", "available")):
            v = _g(acc, *path)
            if isinstance(v, (int, float)):
                return float(v), True, svc.acc_id
        v = svc.bridge.balance()
        try:
            return float(v), False, svc.acc_id
        except (TypeError, ValueError):
            return None, False, svc.acc_id

    def plan(self, platform) -> dict:
        """Сколько можно вывести сейчас по правилам — без вывода."""
        w = self.cfg(platform)
        avail, exact, acc = self.available(platform)
        res = {"platform": platform, "account": acc, "available": avail, "exact": exact, "amount": 0, "reason": ""}
        if avail is None:
            res["reason"] = "не удалось узнать баланс"
            return res
        if not w.get("wallet"):
            res["reason"] = "не указаны реквизиты"
            return res
        if avail < float(w.get("threshold") or 0):
            res["reason"] = f"баланс меньше порога {w.get('threshold')}"
            return res
        amount = math.floor(avail - float(w.get("keep") or 0))
        left_today = float(w.get("max_per_day") or 0) - self.withdrawn_today(platform)
        if float(w.get("max_per_day") or 0) > 0:
            amount = min(amount, math.floor(left_today))
        if amount < float(w.get("min_amount") or 0) or amount <= 0:
            res["reason"] = "сумма меньше минимальной или исчерпан дневной лимит"
            return res
        res["amount"] = amount
        return res

    # ---------- планировщик ----------
    def tick(self):
        for platform in ("funpay", "playerok"):
            w = self.cfg(platform)
            mode = w.get("mode", "off")
            if mode == "off" or self.paused(platform):
                continue
            last = self.app.state.get("flags", f"wd_check:{platform}") or 0
            if time.time() - float(last) < float(w.get("check_every_hours") or 6) * 3600:
                continue
            self.app.state.mark("flags", f"wd_check:{platform}", time.time())
            pend = self.app.state.get("flags", f"wd_pending:{platform}")
            if pend and self.app.state.get("pending", pend):
                continue  # уже ждём ответа владельца
            plan = self.plan(platform)
            if not plan["amount"]:
                log.info("💸 Автовывод %s: пропуск — %s", platform, plan["reason"])
                continue
            if mode == "auto" and plan["exact"]:
                self.execute(platform, plan["account"], plan["amount"], auto=True)
            else:
                self.ask(platform, plan)

    def ask(self, platform, plan):
        w = self.cfg(platform)
        nonce = secrets.token_hex(5)
        self.app.state.mark("pending", nonce, {"action": "withdraw", "platform": platform, "account": plan["account"],
                                                "amount": plan["amount"], "exp": time.time() + 6 * 3600})
        self.app.state.mark("flags", f"wd_pending:{platform}", nonce)
        name = "FunPay" if platform == "funpay" else "Playerok"
        approx = "" if plan["exact"] else "\n<i>Доступный баланс определён приблизительно — проверьте на сайте.</i>"
        self.app.notify("withdraw", f"💸 <b>Вывод с {name}</b> ({esc(self.app.account_name(plan['account']))})\n"
                                    f"Баланс: {plan['available']}\nК выводу: <b>{plan['amount']}</b> на "
                                    f"{esc(w.get('method') or w.get('provider'))} {mask_wallet(w.get('wallet'))}{approx}",
                        buttons=[[("✅ Вывести", f"duo:wd:{nonce}:y", "withdraw"),
                                  ("Пропустить", f"duo:wd:{nonce}:n", "withdraw")]])

    def resolve(self, nonce, yes: bool, by_uid=None) -> str:
        with self.lock:
            p = self.app.state.pop("pending", nonce)
        if not p or p.get("action") != "withdraw" or p.get("exp", 0) < time.time():
            return "Заявка уже обработана или устарела"
        self.app.state.pop("flags", f"wd_pending:{p['platform']}")
        if not yes:
            return "Вывод пропущен"
        return self.execute(p["platform"], p["account"], p["amount"], by=by_uid)

    def execute(self, platform, account, amount, auto=False, by=None) -> str:
        w = self.cfg(platform)
        name = "FunPay" if platform == "funpay" else "Playerok"
        with self.lock:
            cap = float(w.get("max_per_day") or 0)
            if cap and self.withdrawn_today(platform) + amount > cap:
                return f"Превышен дневной лимит {cap}"
            pool = self.app.fp if platform == "funpay" else self.app.pl
            svc = pool.get(account) if account else None
            if not svc or not svc.ok:
                return "Аккаунт не подключён"
            key = self.log_entry(platform, account, amount, "started", f"by {by or ('auto' if auto else '?')}")
            try:
                if platform == "funpay":
                    res = svc.client.withdraw(w.get("currency", "rub"), w.get("method", "card_rub"), w["wallet"], amount)
                else:
                    res = svc.bridge.withdraw(w.get("provider", ""), w["wallet"], amount, w.get("sbp_bank", ""))
            except Exception as e:
                self.app.state.data["withdraw_log"][key].update(status="error", info=str(e)[:300])
                self.app.state.save()
                self.app.state.mark("flags", f"wd_paused:{platform}", str(e)[:200])
                log.error("💸 Вывод %s %s: %s", name, amount, e)
                self.app.notify("withdraw", f"❌ <b>Вывод с {name} не выполнен</b>: {esc(e)}\n"
                                            f"Автовывод приостановлен. Проверьте баланс на сайте и снимите паузу в мини-аппе.")
                return f"Ошибка: {e}"
            self.app.state.data["withdraw_log"][key].update(status="done", info=str(res)[:300])
            self.app.state.save()
            log.info("💸 Вывод %s: %s на %s", name, amount, mask_wallet(w.get("wallet")))
            self.app.notify("withdraw", f"✅ <b>Заявка на вывод создана</b>: {amount} с {name} на "
                                        f"{esc(w.get('method') or w.get('provider'))} {mask_wallet(w.get('wallet'))}")
            return f"Заявка на вывод {amount} создана"
