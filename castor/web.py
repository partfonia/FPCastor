"""Веб-панель и Telegram Mini App: статика из duo/webapp + JSON API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Optional

import requests

from .core import (BACKUP_DIR, BIN_DIR, LOG_BUFFER, PROXY_KINDS, WEBAPP_DIR, Products, UOrder, canonical_proxy,
                   check_proxy, describe_proxy, log, make_backup, parse_proxy, proxy_kind, proxy_label, split_proxies,
                   strip_finance)
from . import APP_NAME, SIGN, VERSION
from .branding import DEFAULT_WATERMARK, WATERMARK_PRESETS, links
from .roles import ROLE_HINTS, ROLE_NAMES, ROLES, perms_of

if TYPE_CHECKING:
    from .app import App


def check_webapp_init_data(init_data: str, bot_token: str, max_age=86400) -> Optional[dict]:
    """Проверка подписи Telegram WebApp initData (HMAC-SHA256, по документации Telegram)."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    recv_hash = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    if max_age and time.time() - int(data.get("auth_date", 0)) > max_age:
        return None
    try:
        return json.loads(data.get("user", "{}"))
    except Exception:
        return None


class WebServer:
    def __init__(self, app: "App"):
        self.app = app
        self.sessions: dict = {}
        self.httpd = None
        self.tunnel_proc = None
        self.tunnel = {"state": "off", "error": "", "checked": 0}  # off · starting · ok · unreachable · failed
        self._fails: dict = {}

    def start(self):
        host, port = self.app.cfg.get("web.host", "0.0.0.0"), int(self.app.cfg.get("web.port", 8765))
        try:
            self.httpd = ThreadingHTTPServer((host, port), self._make_handler())
        except OSError as e:
            log.error("🌐 Веб-панель: порт %s занят (%s)", port, e)
            return
        threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True).start()
        log.info("🌐 Веб-панель: http://127.0.0.1:%s", port)
        if self.app.cfg.get("web.tunnel") == "cloudflared":
            self.start_tunnel(port)

    def stop(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
            if self.tunnel_proc:
                self.tunnel_proc.terminate()
        except Exception:
            pass

    # ═════════ туннель cloudflared — бесплатный HTTPS для мини-аппа ═════════
    def _cloudflared_bin(self) -> Optional[str]:
        found = shutil.which("cloudflared")
        if found:
            return found
        sysname, mach = platform.system().lower(), platform.machine().lower()
        arch = "arm64" if mach in ("aarch64", "arm64") else ("arm" if mach.startswith("arm") else "amd64")
        base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        if sysname == "windows":
            fname, url = "cloudflared.exe", base + f"cloudflared-windows-{arch}.exe"
        elif sysname == "linux":
            fname, url = "cloudflared", base + f"cloudflared-linux-{arch}"
        else:
            log.warning("cloudflared: установите вручную (brew install cloudflared)")
            return None
        dst = BIN_DIR / fname
        if not dst.exists():
            log.info("⬇️ Скачиваю cloudflared…")
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(dst, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                dst.chmod(0o755)
            except Exception as e:
                log.error("cloudflared: не скачался: %s", e)
                dst.unlink(missing_ok=True)
                return None
        return str(dst)

    def start_tunnel(self, port):
        def run():
            self.tunnel.update(state="starting", error="")
            binp = self._cloudflared_bin()
            if not binp:
                self.tunnel.update(state="failed", error="cloudflared не скачался — проверьте интернет")
                return
            while self.app.running:
                try:
                    self.tunnel.update(state="starting", error="")
                    self.tunnel_proc = subprocess.Popen(
                        [binp, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                        errors="replace", **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                    for line in self.tunnel_proc.stdout:
                        m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
                        if m and m.group(0) != self.app.public_url:
                            self.app.public_url = m.group(0)
                            threading.Thread(target=self._announce, daemon=True).start()
                    self.tunnel_proc.wait()
                except Exception as e:
                    log.error("cloudflared: %s", e)
                if self.app.running:
                    self.tunnel.update(state="starting", error="туннель перезапускается")
                    log.warning("cloudflared завершился, перезапуск через 10 с")
                    time.sleep(10)
        threading.Thread(target=run, name="tunnel", daemon=True).start()

    def check_tunnel(self, attempts=1, pause=5) -> bool:
        """Открывается ли мини-апп снаружи (через адрес туннеля)."""
        url = self.app.public_url
        if not url.startswith("https://"):
            self.tunnel.update(state="off" if not url else "failed", error="нет HTTPS-адреса")
            return False
        err = ""
        for i in range(attempts):
            try:
                r = requests.get(url + "/api/health", timeout=10)
                if r.ok and r.json().get("ok"):
                    self.tunnel.update(state="ok", error="", checked=time.time())
                    return True
                err = f"ответ {r.status_code}"
            except requests.Timeout:
                err = "не ответил за 10 с"
            except Exception:
                err = "не удалось подключиться"
            if i < attempts - 1:
                time.sleep(pause)
        self.tunnel.update(state="unreachable", error=err, checked=time.time())
        return False

    def _announce(self):
        """Новый адрес: ждём, пока туннель реально заработает, и только потом ставим кнопку."""
        url = self.app.public_url
        ok = self.check_tunnel(attempts=12, pause=5)  # Cloudflare публикует адрес до минуты
        if url != self.app.public_url:
            return
        if self.app.tg:
            self.app.tg.setup_menu_button()
        if ok:
            log.info("📱 Мини-апп работает: %s", url)
            self.app.notify("start", "📱 <b>Мини-апп готов.</b> Открывайте кнопкой «Панель» слева от поля ввода "
                                     "или из меню /menu.\n<i>При каждом запуске адрес новый — старые кнопки не работают.</i>",
                            buttons=[[("📱 Открыть панель", ("webapp", url))]])
        else:
            log.warning("📱 Мини-апп: адрес %s не открывается (%s). Если вы в России — Cloudflare может "
                        "тормозиться провайдером, включите VPN на телефоне.", url, self.tunnel["error"])

    def restart_tunnel(self):
        self.app.public_url = ""
        self.tunnel.update(state="starting", error="")
        try:
            if self.tunnel_proc:
                self.tunnel_proc.terminate()
        except Exception:
            pass

    # ═════════ сессии и права ═════════
    def new_session(self, uid, role):
        t = hashlib.sha256(os.urandom(32)).hexdigest()
        self.sessions[t] = {"uid": uid, "role": role, "exp": time.time() + 86400 * 3}
        return t

    def session(self, token) -> Optional[dict]:
        s = self.sessions.get(token or "")
        if not s or s["exp"] < time.time():
            return None
        if s["uid"]:  # роль TG-пользователя могли поменять — берём актуальную
            s["role"] = self.app.team.role(s["uid"])
            if not s["role"]:
                return None
        return s

    def valid(self, token) -> bool:
        return self.session(token) is not None

    # ═════════ API ═════════
    GET_PERM = {"me": None, "status": "orders", "config": "settings", "events": "chat", "logs": "system",
                "plugins": "plugins", "products": "products", "orders": "orders", "customers": "customers",
                "chats": "chat", "chat": "chat", "proxy": "system", "backups": "secrets", "lots": "delivery",
                "suppliers/pending": "delivery", "withdraw": "withdraw", "finance": "finance", "team": "users"}
    POST_PERM = {"config": "settings", "send": "chat", "products": "products", "products/delete": "delivery",
                 "plugins/toggle": "plugins", "plugins/delete": "plugins", "plugins/reload": "plugins",
                 "plugins/upload": "plugins", "plugins/url": "plugins", "customers/note": "customers",
                 "telegram/test_token": "system", "telegram/apply": "system", "proxy/test": "system",
                 "proxy/parse": "system", "proxy/kind": "system",
                 "action": None, "lots/set": "delivery", "supplier/preview": "delivery", "supplier/balance": "delivery",
                 "supplier/test": "delivery", "supplier/resolve": "delivery", "withdraw/run": "withdraw",
                 "withdraw/unpause": "withdraw", "withdraw/plan": "withdraw", "order/cost": "finance",
                 "order/refund": "refund", "team/invite": "users", "team/role": "users", "team/remove": "users",
                 "accounts/sync": "settings"}
    ACTION_PERM = {"raise": "settings", "restart": "system", "backup": "secrets", "vacation": "settings",
                   "test_delivery": "delivery", "report": "finance"}

    def api_get(self, p: str, q: dict, sess: dict):
        app = self.app
        perms = perms_of(sess["role"])
        fin = "finance" in perms
        out = lambda x: x if fin else strip_finance(x)  # noqa: E731
        if p == "me":
            return {"role": sess["role"], "role_name": ROLE_NAMES.get(sess["role"]), "perms": sorted(perms),
                    "uid": sess["uid"], "templates": app.cfg.get("templates", []),
                    "app": {"name": APP_NAME, "version": VERSION, "sign": SIGN,
                            "links": [{"key": k, "emoji": e, "name": n, "text": t, "url": u} for k, e, n, t, u in links(app.cfg)],
                            "watermarks": WATERMARK_PRESETS, "default_watermark": DEFAULT_WATERMARK},
                    "accounts": [{"id": a["id"], "platform": a["platform"], "name": a["name"]}
                                 for a in app.accounts_status()]}
        if p == "status":
            st = app.status()
            st["stats"] = app.stats.summary()
            return out(st)
        if p == "config":
            return {"config": app.cfg.masked(), "has": {
                "tg_token": bool(app.cfg.get("telegram.token")),
                "accounts": {a["id"]: bool(a.get("golden_key") or a.get("token") or a.get("cookies"))
                             for pl in ("funpay", "playerok") for a in app.cfg.accounts(pl)}},
                "withdraw_providers": self._pl_providers()}
        if p == "events":
            return out({"events": list(app.events)[::-1][:200]})
        if p == "logs":
            lvl = q.get("level", "")
            lines = list(LOG_BUFFER)[-400:]
            if lvl:
                lines = [x for x in lines if f"[{lvl}]" in x]
            return {"logs": lines}
        if p == "plugins":
            return {"plugins": [i.to_dict(app.plugins.enabled(u)) for u, i in app.plugins.plugins.items()],
                    "failed": app.plugins.failed}
        if p == "products":
            if q.get("file"):
                return {"content": Products.read(q["file"]), "count": Products.count(q["file"])}
            return {"products": Products.list()}
        if p == "orders":
            return out({"orders": app.stats.search(q.get("q", ""), q.get("platform", ""), account=q.get("account", ""))})
        if p == "customers":
            return out({"customers": app.customers.list(q.get("q", ""))})
        if p == "finance":
            st = app.stats
            return {"periods": {k: st.period(sec) for k, sec in (("day", 86400), ("week", 604800),
                                                                 ("month", 2592000), ("all", 10 ** 12))},
                    "today": st.today(), "daily": st.daily(30), "top": st.top_products(15),
                    "by_account": st.by_account(), "names": {a["id"]: a["name"] for a in app.accounts_status()}}
        if p == "chats":
            plat, acc = q.get("platform", "funpay"), q.get("account") or None
            svc = app.service(plat, acc)
            if not svc or not svc.ok:
                return {"chats": [], "error": f"{'FunPay' if plat == 'funpay' else 'Playerok'} не подключён"}
            chats = [dict(c, id=str(c["id"])) for c in svc.client.get_chats()] if plat == "funpay" \
                else svc.bridge.get_chats()
            return {"chats": chats, "account": svc.acc_id}
        if p == "chat":
            plat, cid, acc = q.get("platform", "funpay"), q.get("id", ""), q.get("account") or None
            svc = app.service(plat, acc)
            if plat == "funpay" and svc:
                msgs = svc.client.get_chat_history(int(cid) if cid.isdigit() else cid)
                return {"messages": [{"id": m.id, "author": m.author, "text": m.text or (m.image_link or ""),
                                      "by_me": str(m.author_id) == str(svc.client.id),
                                      "system": str(m.author_id) == "0"} for m in msgs], "account": svc.acc_id}
            if plat == "playerok" and svc:
                return {"messages": svc.bridge.get_chat_messages(cid), "account": svc.acc_id}
            return {"messages": []}
        if p == "proxy":
            pm = app.proxy
            return {"current": pm.current, "label": pm.label(), "results": dict(pm.results),
                    "telegram": app.status()["telegram"], "kinds": PROXY_KINDS,
                    "info": {x: {"label": proxy_label(x), "desc": describe_proxy(x), "kind": proxy_kind(x)}
                             for x in cfg.get("telegram.proxy.list", [])}}
        if p == "backups":
            return {"backups": [{"name": f.name, "size": f.stat().st_size, "t": f.stat().st_mtime}
                                for f in sorted(BACKUP_DIR.glob("backup_*.zip"), reverse=True)]}
        if p == "lots":
            res, errors = [], []
            for svc in list(app.fp.values()):
                if not svc.ok:
                    continue
                extra = [h.get("node") for h in app.state.data.get("hidden_lots", {}).values() if h.get("node")]
                try:
                    for lot in svc.client.get_my_lots(extra):
                        res.append(dict(lot, account=svc.acc_id, account_name=app.account_name(svc.acc_id)))
                except Exception as e:
                    errors.append(f"{svc.acc_id}: {e}")
            return {"lots": res, "errors": errors,
                    "hidden": list(app.state.data.get("hidden_lots", {}).values())}
        if p == "suppliers/pending":
            return {"pending": app.suppliers.pending()}
        if p == "withdraw":
            return {"history": app.withdraw.history(),
                    "plans": {pl: app.withdraw.plan(pl) for pl in ("funpay", "playerok")},
                    "paused": {pl: app.withdraw.paused(pl) for pl in ("funpay", "playerok")},
                    "providers": self._pl_providers()}
        if p == "team":
            return {"users": app.team.users(), "roles": [{"id": r, "name": ROLE_NAMES[r], "hint": ROLE_HINTS[r]}
                                                         for r in ROLES],
                    "bot": app.tg.username if app.tg else None}
        return None

    def _pl_providers(self):
        svc = self.app.service("playerok")
        try:
            return svc.bridge.withdraw_providers() if svc and svc.bridge.ok else []
        except Exception:
            return []

    def api_post(self, p: str, b: dict, sess: dict):
        app = self.app
        cfg = app.cfg
        perms = perms_of(sess["role"])
        who = sess["uid"] or "web"
        if p == "config":
            protect = []
            if "secrets" not in perms:
                protect.append("secrets")
            if "users" not in perms:
                protect += ["telegram.users", "telegram.password"]
            if "withdraw" not in perms:
                protect.append("withdraw")
            if "system" not in perms:
                protect += ["telegram.proxy", "telegram.enabled", "web", "telegram.extra_chats"]
            if "finance" not in perms:
                protect.append("finance")
            cfg.update_from_masked(b.get("config") or {}, tuple(protect))
            log.info("⚙️ Настройки изменены (%s)", who)
            return {"ok": True}
        if p == "send":
            cid = b.get("chat_id")
            cid = int(cid) if str(cid).isdigit() else cid
            return {"ok": app.send(b.get("platform"), cid, b.get("text", ""), account=b.get("account") or None,
                                   manual=True)}
        if p == "products":
            n = Products.add(b["file"], str(b.get("content", "")).splitlines(),
                             replace=not b.get("append") and "delivery" in perms)
            app.lowstock.check(Products.safe_name(b["file"]))
            return {"ok": True, "count": n}
        if p == "products/delete":
            Products.delete(b["file"])
            return {"ok": True}
        if p == "plugins/toggle":
            return {"ok": True, "enabled": app.plugins.toggle(b["uuid"])}
        if p == "plugins/delete":
            return {"ok": app.plugins.delete(b["uuid"])}
        if p == "plugins/reload":
            info = app.plugins.reload(b["uuid"])
            return {"ok": True} if info else {"error": "Не перезагрузился, смотрите логи"}
        if p in ("plugins/upload", "plugins/url"):
            if p == "plugins/upload":
                info = app.plugins.load(app.plugins.save_upload(b["name"], base64.b64decode(b["content"])))
            else:
                info = app.plugins.install_from_url(b["url"])
            return {"ok": True, "name": info.name} if info else {"error": "Плагин сохранён, но не загрузился — смотрите логи"}
        if p == "customers/note":
            app.customers.set_note(b["platform"], b["name"], b.get("note", ""))
            return {"ok": True}
        if p == "telegram/test_token":
            tok = b.get("token", "").strip() or cfg.get("telegram.token")
            try:
                r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10,
                                 proxies=(app.proxy.apihelper_value() or None))
                j = r.json()
                return {"ok": True, "username": j["result"]["username"]} if j.get("ok") else \
                    {"error": j.get("description", "Токен не подошёл")}
            except Exception as e:
                return {"error": f"Нет связи с Telegram: {e}"}
        if p == "telegram/apply":
            if b.get("token"):
                if "secrets" not in perms:
                    return {"error": "Сменить бота может только владелец"}
                cfg.set("telegram.token", b["token"].strip(), save=False)
            if "proxy" in b:
                cur = cfg.get("telegram.proxy", {})
                cur.update({k: v for k, v in b["proxy"].items() if k in ("enabled", "list", "failover",
                                                                         "check_on_start", "allow_direct")})
                cur["list"] = [canonical_proxy(x) if parse_proxy(x) else x for x in cur.get("list", []) if str(x).strip()]
                cfg.set("telegram.proxy", cur, save=False)
            if "enabled" in b:
                cfg.set("telegram.enabled", bool(b["enabled"]), save=False)
            cfg.save()
            ok, info = app.reload_telegram()
            return {"ok": ok, "message": info}
        if p == "proxy/parse":
            good, bad = split_proxies(b.get("text", ""), b.get("kind") or None)
            return {"items": [{"proxy": x, "label": proxy_label(x), "desc": describe_proxy(x)} for x in good],
                    "bad": bad}
        if p == "proxy/kind":
            lst = cfg.get("telegram.proxy.list", [])
            i = int(b.get("i", -1))
            if not 0 <= i < len(lst) or b.get("kind") not in PROXY_KINDS:
                return {"error": "Нет такого прокси"}
            old, lst[i] = lst[i], canonical_proxy(lst[i], b["kind"])
            cfg.set("telegram.proxy.list", lst)
            if app.proxy.current == old:
                app.proxy.current = lst[i]
            return {"ok": True, "proxy": lst[i]}
        if p == "proxy/test":
            res = []
            for px in b.get("list", []):
                ok, info = app.proxy.test(px)
                res.append({"proxy": px, "label": proxy_label(px), "ok": ok, "info": info})
            if b.get("direct", True):
                ok, info = check_proxy("")
                res.append({"proxy": "", "label": "напрямую", "ok": ok, "info": info})
            return {"results": res}
        if p == "accounts/sync":
            app.sync_accounts()
            return {"ok": True}
        if p == "lots/set":
            res = app.stock.set_lots(b.get("lots", []), bool(b.get("active")), b.get("file", ""))
            return {"ok": True, "message": re.sub(r"<[^>]+>", "", res)}
        if p == "supplier/preview":
            return {"request": app.suppliers.preview(b["id"], b.get("product_id", "TEST"), int(b.get("quantity", 1)))}
        if p == "supplier/balance":
            return {"message": "Баланс: " + app.suppliers.balance(b["id"])}
        if p == "supplier/test":
            o = UOrder("funpay", f"TEST-{int(time.time())}", "Тестовая покупка", 0, "test", None, None,
                       int(b.get("quantity", 1)), account="test")
            items, cost = app.suppliers.buy(b["id"], str(b.get("product_id", "")), o.amount, o)
            log.info("🏭 Тестовая покупка у %s (%s): %s шт.", b["id"], who, len(items))
            return {"items": items, "cost": cost}
        if p == "supplier/resolve":
            app.suppliers.resolve_pending(b["key"])
            return {"ok": True}
        if p == "withdraw/plan":
            return {"plan": app.withdraw.plan(b["platform"])}
        if p == "withdraw/run":
            plat = b["platform"]
            plan = app.withdraw.plan(plat)
            amount = int(float(b.get("amount") or 0))
            if amount <= 0:
                return {"error": "Укажите сумму"}
            if not plan.get("account"):
                return {"error": "Аккаунт площадки не подключён"}
            if not cfg.get(f"withdraw.{plat}.wallet"):
                return {"error": "Не указаны реквизиты"}
            return {"message": app.withdraw.execute(plat, plan["account"], amount, by=who)}
        if p == "withdraw/unpause":
            app.withdraw.unpause(b["platform"])
            return {"ok": True}
        if p == "order/cost":
            o = app.stats.set_order_flag(b["id"], "cost", round(float(b.get("cost") or 0), 2), b.get("platform"))
            return {"ok": bool(o)}
        if p == "order/refund":
            svc = app.service("funpay", b.get("account"))
            if not svc:
                return {"error": "Аккаунт FunPay не подключён"}
            svc.client.refund(b["id"])
            app.stats.inc("refunds")
            app.stats.set_order_flag(b["id"], "refunded", True, "funpay")
            log.info("💸 Возврат %s (%s)", b["id"], who)
            return {"ok": True}
        if p == "team/invite":
            if b.get("role") not in ROLES:
                return {"error": "Неизвестная роль"}
            code = app.team.invite(b["role"], who)
            bot = app.tg.username if app.tg else None
            return {"link": f"https://t.me/{bot}?start=inv_{code}" if bot else "", "code": code}
        if p == "team/role":
            return {"ok": True} if app.team.set_role(int(b["id"]), b["role"]) else \
                {"error": "Нельзя: должен остаться хотя бы один владелец"}
        if p == "team/remove":
            return {"ok": True} if app.team.remove(int(b["id"])) else {"error": "Нельзя удалить последнего владельца"}
        if p == "action":
            a = b.get("action")
            need = self.ACTION_PERM.get(a, "system")
            if need not in perms:
                return {"error": "Недостаточно прав"}
            if a == "raise":
                rep = []
                for svc in list(app.fp.values()):
                    if svc.ok:
                        rep += svc.raise_all(force=True)
                return {"message": " | ".join(rep) or "FunPay не подключён или лотов нет"}
            if a == "restart":
                threading.Thread(target=lambda: (time.sleep(1), app.restart()), daemon=True).start()
                return {"message": "Перезапускаюсь…"}
            if a == "backup":
                path = make_backup()
                if app.tg and app.tg.bot:
                    for uid in app.team.owners():
                        try:
                            with open(path, "rb") as f:
                                app.tg.bot.send_document(uid, f, caption="💾 Бекап Castor (содержит ключи)",
                                                         visible_file_name=path.name)
                        except Exception:
                            pass
                return {"message": f"Бекап {path.name} создан и отправлен владельцам в Telegram", "name": path.name}
            if a == "vacation":
                return {"message": "Отпуск включён" if cfg.toggle("features.vacation.enabled") else "Отпуск выключен"}
            if a == "test_delivery":
                o = UOrder(b.get("platform", "funpay"), "TEST", b.get("title", ""), 0, "Покупатель", None, None, 1,
                           account=b.get("account") or None)
                rule = app.find_rule(o)
                if not rule:
                    return {"message": "Ни одно правило не подходит под это название"}
                src = {"file": rule.get("file"), "text": "текст", "supplier": f"поставщик {rule.get('supplier')}"}
                return {"message": f"Сработает правило «{rule.get('keyword')}» → {src.get(rule.get('mode', 'file'))}"}
            if a == "report":
                app.notify("report", app.scheduler.report_text())
                return {"message": "Отчёт отправлен в Telegram"}
        return None

    def _make_handler(self):
        srv = self
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            server_version = "FPCastor"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
                data = body if isinstance(body, bytes) else (
                    body.encode() if isinstance(body, str) else
                    json.dumps(body, ensure_ascii=False, default=str).encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 8_000_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    return {}

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if not u.path.startswith("/api/"):
                    rel = u.path.lstrip("/") or "index.html"
                    f = (WEBAPP_DIR / rel).resolve()
                    if WEBAPP_DIR.resolve() not in f.parents or not f.is_file():
                        f = WEBAPP_DIR / "index.html"
                    ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                    if ctype.startswith("text/") or ctype.endswith("javascript"):
                        ctype += "; charset=utf-8"
                    return self._send(200, f.read_bytes(), ctype)
                p = u.path[5:]
                if p == "health":
                    return self._send(200, {"ok": True})
                token = self.headers.get("X-Token") or q.get("t")
                sess = srv.session(token)
                if not sess:
                    return self._send(401, {"error": "auth"})
                need = srv.GET_PERM.get(p, "secrets" if p == "backup/download" else "system")
                if need and need not in perms_of(sess["role"]):
                    return self._send(403, {"error": "Недостаточно прав"})
                if p == "backup/download":
                    f = BACKUP_DIR / os.path.basename(q.get("file", ""))
                    if not f.is_file():
                        return self._send(404, {"error": "нет файла"})
                    return self._send(200, f.read_bytes(), "application/zip",
                                      {"Content-Disposition": f'attachment; filename="{f.name}"'})
                try:
                    res = srv.api_get(p, q, sess)
                except Exception as e:
                    log.debug(traceback.format_exc())
                    return self._send(500, {"error": str(e)})
                self._send(200, res) if res is not None else self._send(404, {"error": "not found"})

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                p = u.path[5:] if u.path.startswith("/api/") else ""
                b = self._body()
                if p == "auth":
                    ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
                    fails = [t for t in srv._fails.get(ip, []) if time.time() - t < 600]
                    if len(fails) >= 8:
                        return self._send(429, {"error": "Слишком много попыток. Подождите 10 минут."})
                    if b.get("initData"):
                        user = check_webapp_init_data(b["initData"], app.cfg.get("telegram.token", ""))
                        if not user:
                            return self._send(200, {"error": "Telegram не подтвердил вход. Откройте панель кнопкой "
                                                             "из этого бота (не пересланной ссылкой)."})
                        role = app.team.role(user["id"])
                        if role:
                            return self._send(200, {"token": srv.new_session(int(user["id"]), role),
                                                    "user": user.get("first_name", "")})
                        return self._send(200, {"error": f"Вас нет в команде этого бота (ID {user['id']}). "
                                                         f"Попросите владельца прислать приглашение."})
                    pwd = app.cfg.get("web.password") or app.cfg.get("telegram.password")
                    if pwd and b.get("password") and hmac.compare_digest(str(b["password"]), str(pwd)):
                        return self._send(200, {"token": srv.new_session(0, "owner")})
                    srv._fails[ip] = fails + [time.time()]
                    return self._send(200, {"error": "Неверный пароль"})
                sess = srv.session(self.headers.get("X-Token"))
                if not sess:
                    return self._send(401, {"error": "auth"})
                need = srv.POST_PERM.get(p, "system")
                if need and need not in perms_of(sess["role"]):
                    return self._send(200, {"error": "Недостаточно прав для этого действия"})
                try:
                    res = srv.api_post(p, b, sess)
                except Exception as e:
                    log.debug(traceback.format_exc())
                    return self._send(200, {"error": str(e)})
                self._send(200, res) if res is not None else self._send(404, {"error": "not found"})
        return Handler
