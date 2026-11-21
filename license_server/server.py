"""Сервер лицензий и магазин платных плагинов Castor.

Что делает:
  • HTTP API /api/check — проверяет ключ и отвечает ПОДПИСАННЫМ ответом (Ed25519). Плагин у покупателя
    проверяет подпись открытым ключом, поэтому подменить сервер или ответ нельзя.
  • Telegram-магазин: каталог, оплата Telegram Stars, моментальная выдача ключа, продление,
    «мои ключи», сброс привязки к серверу. Команды владельца: /grant /keys /revoke /reset /stats.

Запуск: python server.py   (первый запуск создаст data/config.json и ключи подписи)
Нужны пакеты: pyTelegramBotAPI, cryptography.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CFG_PATH = DATA / "config.json"
KEY_PATH = DATA / "signing_key.b64"     # ЗАКРЫТЫЙ ключ: никому не показывать, не выкладывать на GitHub
DB_PATH = DATA / "licenses.db"

DEFAULT_CFG = {
    "shop_bot_token": "",                 # токен отдельного бота-магазина от @BotFather
    "owners": [],                         # ваш Telegram ID (узнать: @userinfobot)
    "port": 8800,                         # на хостинге с панелью берётся из SERVER_PORT
    "public_url": "",                     # адрес сервера для плагинов: http://IP:ПОРТ
    "proxy": "",                          # прокси для Telegram, если нужен
    "max_machines": 1,                    # на скольких установках бота можно использовать один ключ
    "reset_cooldown_hours": 24,           # как часто покупатель может сам сбросить привязку
    "products": {
        "example_plugin": {"name": "Пример плагина", "description": "Что умеет плагин",
                           "stars": 100, "days": 30, "file": ""}
    },
    "welcome": "Магазин плагинов для FunPay & Playerok Castor 🛍\nВыберите плагин — оплата звёздами Telegram, ключ придёт сразу.",
}


# ═══════════════════════ хранилище ═══════════════════════
def load_cfg() -> dict:
    if not CFG_PATH.exists():
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Создан {CFG_PATH} — заполните shop_bot_token, owners, public_url и products, затем перезапустите.")
    cfg = dict(DEFAULT_CFG, **json.loads(CFG_PATH.read_text(encoding="utf-8")))
    return cfg


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS licenses(
        key TEXT PRIMARY KEY, plugin TEXT, tg_id INTEGER, expires REAL, created REAL,
        active INTEGER DEFAULT 1, note TEXT DEFAULT '', last_reset REAL DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS machines(
        key TEXT, machine TEXT, first REAL, last REAL, PRIMARY KEY(key, machine))""")
    con.execute("""CREATE TABLE IF NOT EXISTS payments(
        charge_id TEXT PRIMARY KEY, tg_id INTEGER, plugin TEXT, stars INTEGER, key TEXT, t REAL)""")
    con.commit()
    return con


DB = None
DB_LOCK = threading.RLock()


def q(sql, args=(), one=False, commit=False):
    with DB_LOCK:
        cur = DB.execute(sql, args)
        if commit:
            DB.commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


# ═══════════════════════ подпись ═══════════════════════
def signing_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    if not KEY_PATH.exists():
        sk = Ed25519PrivateKey.generate()
        raw = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        KEY_PATH.write_text(base64.b64encode(raw).decode())
        try:
            os.chmod(KEY_PATH, 0o600)
        except Exception:
            pass
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEY_PATH.read_text().strip()))


def public_key_b64(sk) -> str:
    from cryptography.hazmat.primitives import serialization
    return base64.b64encode(sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ═══════════════════════ лицензии ═══════════════════════
def new_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "CST-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def grant(tg_id: int, plugin: str, days: float, note="") -> tuple:
    """Новый ключ или продление действующего ключа этого покупателя на этот плагин. → (ключ, истекает)"""
    now = time.time()
    row = q("SELECT * FROM licenses WHERE tg_id=? AND plugin=? AND active=1 ORDER BY expires DESC", (tg_id, plugin), one=True)
    add = days * 86400 if days else 0
    if row:
        if not row["expires"]:
            return row["key"], 0
        exp = 0 if not days else max(row["expires"], now) + add
        q("UPDATE licenses SET expires=? WHERE key=?", (exp, row["key"]), commit=True)
        return row["key"], exp
    key = new_key()
    exp = now + add if days else 0
    q("INSERT INTO licenses(key, plugin, tg_id, expires, created, note) VALUES(?,?,?,?,?,?)",
      (key, plugin, tg_id, exp, now, note), commit=True)
    return key, exp


def check(req: dict, cfg: dict) -> dict:
    key, plugin, machine = str(req.get("key", "")).strip().upper(), str(req.get("plugin", "")), str(req.get("machine", ""))
    owners = [int(x) for x in req.get("owners", []) if str(x).lstrip("-").isdigit()]
    base = {"plugin": plugin, "machine": machine, "nonce": str(req.get("nonce", ""))[:64], "ts": int(time.time())}
    row = q("SELECT * FROM licenses WHERE key=?", (key,), one=True)
    if not row or row["plugin"] != plugin:
        return dict(base, ok=False, reason="ключ не найден — проверьте, что скопировали его полностью")
    if not row["active"]:
        return dict(base, ok=False, reason="ключ отозван")
    if row["expires"] and row["expires"] < time.time():
        return dict(base, ok=False, reason="срок ключа истёк — продлите в магазине", expires=int(row["expires"]))
    if row["tg_id"] and owners and row["tg_id"] not in owners:
        return dict(base, ok=False, reason="ключ куплен другим человеком — он работает только у владельца бота, который его купил")
    if not machine:
        return dict(base, ok=False, reason="нет ID установки")
    known = [r["machine"] for r in q("SELECT machine FROM machines WHERE key=?", (key,))]
    if machine not in known:
        if len(known) >= int(cfg.get("max_machines", 1)):
            return dict(base, ok=False, reason="ключ уже используется на другой установке бота. Сбросить привязку: "
                                                "в магазине /reset или напишите продавцу")
        q("INSERT INTO machines(key, machine, first, last) VALUES(?,?,?,?)", (key, machine, time.time(), time.time()), commit=True)
    else:
        q("UPDATE machines SET last=? WHERE key=? AND machine=?", (time.time(), key, machine), commit=True)
    return dict(base, ok=True, expires=int(row["expires"] or 0), tg_id=row["tg_id"])


# ═══════════════════════ HTTP ═══════════════════════
def make_handler(cfg, sk):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/health"):
                return self._send(200, {"ok": True, "public_key": public_key_b64(sk)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/check"):
                return self._send(404, {"error": "not found"})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 10000)
                req = json.loads(self.rfile.read(n) or b"{}")
                payload = check(req, cfg)
            except Exception as e:
                payload = {"ok": False, "reason": f"ошибка запроса: {e}", "ts": int(time.time()), "nonce": "", "plugin": "", "machine": ""}
            sig = base64.b64encode(sk.sign(canonical(payload))).decode()
            self._send(200, {"payload": payload, "sig": sig})
    return H


# ═══════════════════════ Telegram-магазин ═══════════════════════
def run_shop(cfg):
    import telebot
    from telebot import apihelper
    from telebot.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K, LabeledPrice
    if cfg.get("proxy"):
        apihelper.proxy = {"https": cfg["proxy"], "http": cfg["proxy"]}
    bot = telebot.TeleBot(cfg["shop_bot_token"], parse_mode="HTML")
    owners = {int(x) for x in cfg.get("owners", [])}
    P = cfg["products"]

    def fmt_exp(e):
        return "навсегда" if not e else time.strftime("%d.%m.%Y", time.localtime(e))

    def catalog(uid):
        kb = K()
        for pid, p in P.items():
            kb.add(B(f"{p['name']} — {p['stars']} ⭐", callback_data=f"p:{pid}"))
        kb.add(B("🔑 Мои ключи", callback_data="mine"))
        return kb

    @bot.message_handler(commands=["start", "shop"])
    def start(m):
        bot.send_message(m.chat.id, cfg.get("welcome") or "Магазин плагинов", reply_markup=catalog(m.from_user.id))

    @bot.callback_query_handler(func=lambda c: True)
    def cb(c):
        uid = c.from_user.id
        if c.data.startswith("p:"):
            pid = c.data[2:]
            p = P.get(pid)
            if not p:
                return bot.answer_callback_query(c.id, "Нет такого плагина")
            kb = K().add(B(f"Купить за {p['stars']} ⭐", callback_data=f"buy:{pid}")).add(B("◀️ Назад", callback_data="back"))
            term = f"{p['days']} дн." if p.get("days") else "навсегда"
            bot.edit_message_text(f"<b>{p['name']}</b>\n\n{p.get('description', '')}\n\nСрок: {term}\nЦена: {p['stars']} ⭐",
                                  c.message.chat.id, c.message.message_id, reply_markup=kb)
        elif c.data.startswith("buy:"):
            pid = c.data[4:]
            p = P.get(pid)
            if not p:
                return
            bot.send_invoice(c.message.chat.id, title=p["name"][:32], description=(p.get("description") or p["name"])[:255],
                             invoice_payload=f"{pid}:{uid}", provider_token="", currency="XTR",
                             prices=[LabeledPrice(p["name"][:32], int(p["stars"]))])
            bot.answer_callback_query(c.id)
        elif c.data == "mine":
            rows = q("SELECT * FROM licenses WHERE tg_id=? AND active=1", (uid,))
            text = "<b>Ваши ключи</b>\n\n" + ("\n\n".join(
                f"{P.get(r['plugin'], {}).get('name', r['plugin'])}\n<code>{r['key']}</code>\nдо {fmt_exp(r['expires'])}"
                for r in rows) or "Ключей пока нет.")
            text += "\n\nКлюч вводится в вашем боте Castor: меню → «Далее» → «🔑 Лицензии».\nПеренесли бота на другой сервер — /reset КЛЮЧ"
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                                  reply_markup=K().add(B("◀️ Назад", callback_data="back")))
        elif c.data == "back":
            bot.edit_message_text(cfg.get("welcome") or "Магазин", c.message.chat.id, c.message.message_id, reply_markup=catalog(uid))

    @bot.pre_checkout_query_handler(func=lambda q_: True)
    def pre(qq):
        pid = qq.invoice_payload.split(":")[0]
        bot.answer_pre_checkout_query(qq.id, ok=pid in P, error_message="Плагин недоступен")

    @bot.message_handler(content_types=["successful_payment"])
    def paid(m):
        sp = m.successful_payment
        pid = sp.invoice_payload.split(":")[0]
        if q("SELECT 1 FROM payments WHERE charge_id=?", (sp.telegram_payment_charge_id,), one=True):
            return
        p = P[pid]
        key, exp = grant(m.from_user.id, pid, float(p.get("days") or 0), note="stars")
        q("INSERT INTO payments VALUES(?,?,?,?,?,?)", (sp.telegram_payment_charge_id, m.from_user.id, pid,
                                                      sp.total_amount, key, time.time()), commit=True)
        text = (f"✅ Оплата прошла! <b>{p['name']}</b>\n\nВаш ключ:\n<code>{key}</code>\nДействует до: {fmt_exp(exp)}\n\n"
                f"Как активировать: в боте Castor откройте меню → «Далее» → «🔑 Лицензии» → «{p['name']}» → «Ввести ключ».")
        bot.send_message(m.chat.id, text)
        if p.get("file") and (ROOT / p["file"]).exists():
            with open(ROOT / p["file"], "rb") as f:
                bot.send_document(m.chat.id, f, caption="Файл плагина: отправьте его своему боту Castor или положите в папку plugins")
        for o in owners:
            try:
                bot.send_message(o, f"💰 Продажа: {p['name']} за {sp.total_amount} ⭐ — @{m.from_user.username or m.from_user.id}")
            except Exception:
                pass

    @bot.message_handler(commands=["reset"])
    def reset(m):
        parts = (m.text or "").split()
        if len(parts) < 2:
            return bot.reply_to(m, "Напишите: /reset КЛЮЧ — сброс привязки к серверу (раз в сутки)")
        key = parts[1].strip().upper()
        row = q("SELECT * FROM licenses WHERE key=?", (key,), one=True)
        if not row or (row["tg_id"] != m.from_user.id and m.from_user.id not in owners):
            return bot.reply_to(m, "Ключ не найден среди ваших")
        cd = float(cfg.get("reset_cooldown_hours", 24)) * 3600
        if m.from_user.id not in owners and time.time() - (row["last_reset"] or 0) < cd:
            return bot.reply_to(m, "Сбросить привязку можно раз в сутки. Если срочно — напишите продавцу.")
        q("DELETE FROM machines WHERE key=?", (key,), commit=True)
        q("UPDATE licenses SET last_reset=? WHERE key=?", (time.time(), key), commit=True)
        bot.reply_to(m, "Готово: привязка сброшена. Ключ активируется на той установке бота, где его проверят первой.")

    # ── команды владельца ──
    def owner_only(m):
        return m.from_user.id in owners

    @bot.message_handler(commands=["grant"], func=owner_only)
    def cmd_grant(m):
        p = (m.text or "").split()
        if len(p) < 4 or p[2] not in P:
            return bot.reply_to(m, "/grant TG_ID ПЛАГИН ДНЕЙ  (0 — навсегда)\nПлагины: " + ", ".join(P))
        key, exp = grant(int(p[1]), p[2], float(p[3]), note="grant")
        bot.reply_to(m, f"Ключ: <code>{key}</code> до {fmt_exp(exp)}")
        try:
            bot.send_message(int(p[1]), f"🎁 Вам выдан ключ на «{P[p[2]]['name']}»:\n<code>{key}</code>\nДо: {fmt_exp(exp)}")
        except Exception:
            pass

    @bot.message_handler(commands=["keys"], func=owner_only)
    def cmd_keys(m):
        p = (m.text or "").split()
        rows = q("SELECT * FROM licenses WHERE tg_id=?", (int(p[1]),)) if len(p) > 1 else \
            q("SELECT * FROM licenses ORDER BY created DESC LIMIT 30")
        bot.reply_to(m, "\n".join(f"<code>{r['key']}</code> {r['plugin']} tg:{r['tg_id']} до {fmt_exp(r['expires'])}"
                                  f"{'' if r['active'] else ' ❌'}" for r in rows) or "Нет ключей")

    @bot.message_handler(commands=["revoke"], func=owner_only)
    def cmd_revoke(m):
        p = (m.text or "").split()
        if len(p) < 2:
            return bot.reply_to(m, "/revoke КЛЮЧ")
        q("UPDATE licenses SET active=0 WHERE key=?", (p[1].upper(),), commit=True)
        bot.reply_to(m, "Ключ отозван")

    @bot.message_handler(commands=["stats"], func=owner_only)
    def cmd_stats(m):
        tot = q("SELECT COUNT(*) n, COALESCE(SUM(stars),0) s FROM payments", one=True)
        month = q("SELECT COUNT(*) n, COALESCE(SUM(stars),0) s FROM payments WHERE t>?", (time.time() - 30 * 86400,), one=True)
        act = q("SELECT COUNT(*) n FROM licenses WHERE active=1 AND (expires=0 OR expires>?)", (time.time(),), one=True)
        bot.reply_to(m, f"Продаж всего: {tot['n']} на {tot['s']} ⭐\nЗа 30 дней: {month['n']} на {month['s']} ⭐\n"
                        f"Активных ключей: {act['n']}")

    print("Магазин запущен: @" + bot.get_me().username)
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25, skip_pending=True)
        except Exception as e:
            print("Магазин: ошибка связи с Telegram:", e)
            time.sleep(5)


def main():
    global DB
    cfg = load_cfg()
    DB = db()
    sk = signing_key()
    pub = public_key_b64(sk)
    port = int(os.environ.get("SERVER_PORT") or cfg.get("port") or 8800)
    print("=" * 60)
    print("Сервер лицензий Castor")
    print("ОТКРЫТЫЙ ключ (вставьте в платный плагин, PUBKEY):")
    print("  " + pub)
    print("Адрес для плагинов (SERVER):", cfg.get("public_url") or f"http://<IP-сервера>:{port}")
    print("=" * 60)
    if cfg.get("shop_bot_token"):
        threading.Thread(target=lambda: _safe(run_shop, cfg), daemon=True).start()
    else:
        print("Магазин не запущен: в data/config.json не указан shop_bot_token")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg, sk))
    print(f"API слушает порт {port}")
    httpd.serve_forever()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    if "--grant" in sys.argv:   # python server.py --grant TG_ID ПЛАГИН ДНЕЙ — выдать ключ без Telegram
        i = sys.argv.index("--grant")
        DB = db()
        k, e = grant(int(sys.argv[i + 1]), sys.argv[i + 2], float(sys.argv[i + 3]), note="cli")
        print(k, "до", "навсегда" if not e else time.strftime("%d.%m.%Y", time.localtime(e)))
    else:
        main()
