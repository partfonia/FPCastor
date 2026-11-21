"""Консоль: меню при запуске, мастер настройки, автозапуск, точка входа."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from . import APP_NAME, VERSION
from .bootstrap import REQUIRED_PACKAGES, Spinner, pip_install, playerok_status, PLAYEROK_DEPS
from .branding import BANNER_ART
from .core import (BASE_DIR, CONFIG_PATH, DEFAULT_UA, PLUGINS_DIR, Config, check_proxy, diagnose_proxy, log,
                   normalize_proxy, proxies_dict, proxy_label, rand_password, setup_logging)
from .funpay import FunPayClient
from .playerok import PlayerokBridge

# ═══════════════════════ оформление ═══════════════════════
if os.name == "nt":
    os.system("")  # включает ANSI-цвета в консоли Windows
R, B, D = "\033[0m", "\033[1m", "\033[2m"
CYAN, GREEN, RED, YEL, BLUE, MAG = "\033[96m", "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[95m"
WIDTH = 64

BANNER = (f"{CYAN}{B}{BANNER_ART}{R}  {MAG}♊{R} {D}v{VERSION}{R}\n"
          f"  {B}FunPay{R} & {B}Playerok{R} — бот-помощник продавца\n")


def rule(text="", color=CYAN):
    line = f"━━━ {text} " if text else ""
    print(f"\n{color}{B}{line}{'━' * max(4, WIDTH - len(line))}{R}")


def step(n, total, name, about=""):
    rule(f"Шаг {n} из {total} · {name}")
    if about:
        for ln in about.split("\n"):
            print(f"  {D}{ln}{R}")


def ok(t):
    print(f"  {GREEN}✔{R} {t}")


def bad(t):
    print(f"  {RED}✘{R} {t}")


def warn(t):
    print(f"  {YEL}!{R} {t}")


def info(t):
    print(f"  {BLUE}ℹ{R} {D}{t}{R}")


def _input(prompt) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def ask_yes(question, default=True) -> bool:
    hint = f"{B}Д{R}{D}/н{R}" if default else f"{D}д/{R}{B}Н{R}"
    while True:
        v = _input(f"  {YEL}?{R} {question} ({hint}): ").strip().lower()
        if not v:
            return default
        if v in ("д", "да", "y", "yes", "1", "+", "l", "lf"):   # l/lf — «д»/«да» в английской раскладке
            return True
        if v in ("н", "нет", "n", "no", "0", "-", "т", "ytn"):  # т/ytn — «n»/«нет» не в той раскладке
            return False
        warn("Ответьте «д» (да) или «н» (нет), либо просто Enter")


def ask_text(question, default="", hint="") -> str:
    if hint:
        info(hint)
    d = f" {D}[{default}]{R}" if default else ""
    v = _input(f"  {YEL}?{R} {question}{d}: ").strip()
    return v or default


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    n1 = n % 10
    return many if 10 < n < 20 else few if 1 < n1 < 5 else one if n1 == 1 else many


def human_error(e) -> str:
    """Понятный текст вместо технической ошибки requests."""
    s = str(e)
    low = s.lower()
    if "proxyerror" in low or "unable to connect to proxy" in low:
        return "прокси не пустил запрос — проверьте прокси или подключитесь без него"
    if any(x in low for x in ("max retries", "connectionerror", "connection aborted", "name resolution",
                              "getaddrinfo", "timed out", "сеть:")):
        return "сайт не отвечает — проверьте интернет, включите VPN или прокси"
    if "403" in low or "не авторизован" in low or "unauthorized" in low:
        return "ключ не подошёл — возможно, он устарел: выйдите и снова войдите на сайте и скопируйте новый"
    return s[:200]


def mask(s: str) -> str:
    s = s or ""
    return f"{s[:4]}…{s[-4:]}" if len(s) > 12 else "•" * len(s)


def ask_secret(question, validate=None, hint="", allow_empty=False) -> str:
    """Ввод ключа/токена (виден на экране) + проверка формата."""
    if hint:
        info(hint)
    info("Вставьте и нажмите Enter (правая кнопка мыши или Ctrl+V)")
    while True:
        v = _input(f"  {YEL}?{R} {question}: ").strip()
        if not v:
            if allow_empty:
                return ""
            if not ask_yes("Пусто. Пропустить этот шаг?", True):
                continue
            return ""
        err = validate(v) if validate else None
        if err:
            bad(err)
            if ask_yes("Ввести ещё раз?", True):
                continue
            if not ask_yes("Всё равно сохранить как есть?", False):
                return ""
        ok(f"Принято ({len(v)} {plural(len(v), 'символ', 'символа', 'символов')})")
        return v


def choose(question, options: list, default=1) -> int:
    print(f"  {YEL}?{R} {question}")
    for i, (title, sub) in enumerate(options, 1):
        star = f" {GREEN}← рекомендую{R}" if i == default else ""
        print(f"     {B}{i}{R}. {title}{star}" + (f"\n        {D}{sub}{R}" if sub else ""))
    while True:
        v = _input(f"  {YEL}›{R} Номер {D}[{default}]{R}: ").strip()
        if not v:
            return default
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v)
        warn(f"Введите число от 1 до {len(options)}")


# ═══════════════════════ проверки форматов ═══════════════════════
def v_golden_key(v):
    if re.fullmatch(r"[a-z0-9]{32}", v):
        return None
    return "golden_key — это 32 символа из маленьких латинских букв и цифр. Похоже, скопировано что-то другое"


def extract_pl_token(v: str) -> str:
    m = re.search(r"token=([^;\s]+)", v)
    return m.group(1) if m else v.strip()


def v_pl_token(v):
    t = extract_pl_token(v)
    if t.startswith("eyJ") and t.count(".") == 2:
        return None
    return "token Playerok начинается с «eyJ» и состоит из трёх частей через точку. Похоже, скопировано не то"


def v_tg_token(v):
    if re.fullmatch(r"\d{6,12}:[\w-]{30,}", v):
        return None
    return "токен бота выглядит так: 1234567890:AAH…  (цифры, двоеточие, буквы). Его выдаёт @BotFather"


# ═══════════════════════ мастер настройки ═══════════════════════
TOTAL = 5


def wizard(cfg: Config):
    print(f"\n  {B}🧙 Мастер настройки{R}")
    print(f"  {D}Займёт пару минут. Всё, что вы здесь выберете, потом можно поменять в Telegram или мини-аппе.\n"
          f"  Enter — согласиться с вариантом по умолчанию, Ctrl+C — прервать.{R}")
    try:
        _step_platform(cfg, "funpay", 1)
        _step_platform(cfg, "playerok", 2)
        _step_telegram(cfg, 3)
        _step_webapp(cfg, 4)
        cfg.save()
        _step_summary(cfg, 5)
    except KeyboardInterrupt:
        print()
        cfg.save()
        warn("Мастер прерван. То, что уже введено, сохранено. Запустите снова: python main.py --setup")
        raise SystemExit(0)


# ---------- площадки ----------
def _step_platform(cfg: Config, platform: str, n: int):
    fp = platform == "funpay"
    name = "🔵 FunPay" if fp else "🟢 Playerok"
    step(n, TOTAL, name, "Подключите аккаунт продавца. Можно добавить несколько аккаунтов." if fp else
         "Подключите аккаунт продавца Playerok. Если не торгуете там — просто пропустите.")
    accs = cfg.accounts(platform)
    for a in accs:
        info(f"Уже подключён: {a.get('name') or a['id']}")
    if not ask_yes(f"Добавить аккаунт {'FunPay' if fp else 'Playerok'}?" if accs else
                   f"Подключить {'FunPay' if fp else 'Playerok'}?", default=fp and not accs):
        return
    if not fp:
        info("Проверяю компоненты для Playerok — при первом запуске это может занять до минуты")
        ready, why = playerok_status()
        if not ready:
            bad(f"Playerok сейчас подключить нельзя: {why}")
            info("FunPay и всё остальное будут работать. Playerok можно подключить позже — в мини-аппе или здесь же")
            return
        ok("Компоненты для Playerok на месте")
    while True:
        a = {"id": cfg.new_account_id(platform), "name": "", "enabled": True, "proxy": "", "user_agent": "",
             "auto_delivery": True}
        if fp:
            key = ask_secret("golden_key", v_golden_key,
                             "Где взять: войдите на funpay.com → F12 → вкладка Application → Cookies → funpay.com → golden_key")
            if not key:
                return
            a.update(golden_key=key, auto_raise=True)
        else:
            raw = ask_secret("token", v_pl_token,
                             "Где взять: войдите на playerok.com → F12 → вкладка Application → Cookies → playerok.com → token")
            if not raw:
                return
            a.update(token=extract_pl_token(raw), cookies="")
        if ask_yes("Подключать этот аккаунт через прокси?", False):
            a["proxy"], _ = _ask_proxy("для площадки", "https://funpay.com" if fp else "https://playerok.com")
        uname = None
        with Spinner("Вхожу в аккаунт…"):
            try:
                if fp:
                    c = FunPayClient(a["golden_key"], DEFAULT_UA, a["proxy"]).login()
                    uname, extra = c.username, f"баланс {c.balance} {c.currency}"
                else:
                    b = PlayerokBridge(cfg, a).connect()
                    uname, extra = b.username, ""
                err = None
            except Exception as e:
                err = human_error(e)
        if err:
            bad(f"Не удалось войти: {err}")
            if ask_yes("Ввести ключ заново?", True):
                continue
            if not ask_yes("Сохранить аккаунт как есть и проверить позже?", False):
                return
        else:
            ok(f"Вход выполнен: {B}{uname}{R}" + (f" · {extra}" if extra else ""))
        a["name"] = ask_text("Название аккаунта — видно только вам", uname or f"{'FunPay' if fp else 'Playerok'} {a['id'][2:]}")
        accs.append(a)
        cfg.data["accounts"][platform] = accs
        cfg.data[platform]["enabled"] = True
        ok(f"Аккаунт «{a['name']}» добавлен")
        if not ask_yes("Добавить ещё один аккаунт?", False):
            return


KIND_OPTIONS = [("auto", "Определить автоматически", "бот сам поймёт по адресу и проверке"),
                ("ipv4", "IPv4", "обычный личный прокси — подходит для всего"),
                ("ipv4_shared", "IPv4 Shared", "общий IPv4 на несколько человек — дешевле, может быть медленнее"),
                ("ipv6", "IPv6", "работает только с сайтами, у которых есть IPv6 (Telegram — да)")]


def _read_proxy(purpose: str) -> str:
    """Читает прокси одной строкой или блоком «IP: … / Порт: … / Логин: … / Пароль: …»."""
    first = ask_text(f"Прокси {purpose} (Enter — без прокси)")
    if not first or not re.match(r"(?i)\s*(ip|хост|host|адрес|сервер|server|порт|port|логин|login|"
                                 r"пароль|password|тип|протокол)\b[^:=]*[:=]", first):
        return first
    lines = [first]
    info("Вижу блок «ключ: значение» — вставьте остальные строки, в конце пустая строка")
    while True:
        v = _input(f"  {D}…{R} ").rstrip()
        if not v.strip():
            break
        lines.append(v)
    return "\n".join(lines)


def _ask_proxy(purpose: str, test_url: str) -> tuple:
    """→ (прокси, проверен ли). Пустая строка — без прокси. Запись приводится к нужному формату сама."""
    from .core import _exit_ip, canonical_proxy, describe_proxy, parse_proxy, proxy_kind, scheme_title
    info("Вставьте прокси в любом виде — бот сам приведёт его к нужному формату.")
    info(f"{D}Например: ip:порт:логин:пароль · логин:пароль@ip:порт · socks5://логин:пароль@ip:порт{R}")
    while True:
        raw = _read_proxy(purpose)
        if not raw:
            return "", False
        d = parse_proxy(raw)
        if not d:
            bad("Не получилось разобрать запись — нужны хотя бы IP и порт, например 45.91.209.143:10861")
            continue
        ok(f"Распознал: {describe_proxy(raw)}")
        given = proxy_kind(raw)
        default = next(i for i, (k, _, _) in enumerate(KIND_OPTIONS, 1) if k == given)
        c = choose("Какой это прокси? (как написано у продавца)", [(t, sub) for _, t, sub in KIND_OPTIONS], default)
        kind = KIND_OPTIONS[c - 1][0]
        if kind == "auto" and given == "ipv6":
            kind = "ipv6"  # адрес сам IPv6
        p = canonical_proxy(raw, kind)
        with Spinner("Проверяю прокси — пробую разные типы подключения (до 30 с)…"):
            r = diagnose_proxy(p, test_url)
            if r["ok"] and kind == "auto":
                ip, ver = _exit_ip(r["proxy"])
                if ver == 6:
                    kind, r["kind"] = "ipv6", "ipv6"
        if r["ok"]:
            res = canonical_proxy(r["proxy"], kind)
            note = f" {D}(подключение {scheme_title(r['proxy'])}){R}"
            ok(f"Прокси работает: {proxy_label(res)}, ответ за {r['ms']} мс{note}")
            info(f"{D}Сохраню как: {res}{R}")
            return res, True
        if kind == "auto" and r.get("kind") == "ipv6":
            kind = "ipv6"
            p = canonical_proxy(raw, kind)
            info("Проверка показала, что прокси выходит в интернет через IPv6 — запомнил это")
        bad(r["reason"])
        if not r.get("alive"):
            info("Проверьте у продавца прокси: тип (SOCKS5 или HTTP), порт, срок действия и привязку к вашему IP")
        c = choose("Что дальше?", [("Ввести другой прокси", ""), ("Сохранить этот прокси и проверить при запуске", ""),
                                    ("Продолжить без прокси", "")], default=1)
        if c == 2:
            return p, False
        if c == 3:
            return "", False


# ---------- Telegram ----------
def _tg(token, method, proxy="", **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40,
                      proxies=proxies_dict(proxy))
    return r.json()


def _step_telegram(cfg: Config, n: int):
    t = cfg.data["telegram"]
    step(n, TOTAL, "🤖 Telegram-бот", "Через него приходят уведомления о заказах и сообщениях, и в нём админ-панель.")
    if not ask_yes("Подключить Telegram-бота?", True):
        t["enabled"] = False
        return
    t["enabled"] = True
    if t.get("token") and ask_yes(f"Бот уже подключён ({mask(t['token'])}). Оставить его?", True):
        token = t["token"]
    else:
        info("Как создать бота: откройте @BotFather → /newbot → придумайте имя и юзернейм (на «bot») → скопируйте токен")
        token = ask_secret("Токен бота", v_tg_token)
        if not token:
            t["enabled"] = False
            warn("Без бота не будет уведомлений и админ-панели. Подключить можно позже: python main.py --setup")
            return
    plist = cfg.get("telegram.proxy.list", []) or [""]
    proxy = plist[0] if cfg.get("telegram.proxy.enabled") else ""
    if not t.get("password"):
        t["password"] = rand_password()
    while True:
        with Spinner("Проверяю бота…"):
            try:
                me = _tg(token, "getMe", proxy)
                err = None if me.get("ok") else ("Telegram не принял токен — проверьте, что скопировали его целиком"
                                                 if me.get("error_code") in (401, 404) else me.get("description", "ошибка Telegram"))
            except Exception:
                me, err = {}, "нет связи с Telegram"
        if not err:
            break
        bad(err)
        if "нет связи" in err:
            info("api.telegram.org недоступен с этого компьютера" + (" даже через прокси" if proxy else "") +
                 ". Нужен зарубежный прокси SOCKS5 или HTTP (MTProto не подходит) — или включите VPN на компьютере")
            proxy, verified = _ask_proxy("для Telegram", "https://api.telegram.org")
            if proxy:
                t["proxy"]["list"] = [proxy] + [x for x in t["proxy"].get("list", []) if x != proxy]
                t["proxy"]["enabled"] = True
                if verified:
                    continue  # прокси рабочий — проверяем бота ещё раз
            t["token"] = token  # сохраняем токен, связь проверим при запуске
            warn("Сохранил настройки. Связь с Telegram бот проверит при запуске; прокси можно поменять в меню (пункт 3)")
            print(f"  🔑 Когда бот заработает, напишите ему: {B}/login {t['password']}{R} — станете владельцем")
            return
        if ask_yes("Ввести другой токен?", True):
            token = ask_secret("Токен бота", v_tg_token)
            if token:
                continue
        t["enabled"] = False
        return
    t["token"] = token
    bot = me["result"]["username"]
    ok(f"Бот на связи: {B}@{bot}{R}" + (f" {D}через прокси {proxy_label(proxy)}{R}" if proxy else ""))
    owners = [u for u in t.get("users", []) if u.get("role") == "owner"]
    if owners:
        info(f"Владелец уже назначен (ID {owners[0]['id']})")
        return
    print(f"\n  {B}Сделаем вас владельцем бота.{R}")
    print(f"  Откройте в Telegram {CYAN}https://t.me/{bot}{R} и нажмите {B}Start{R} (или отправьте /start).")
    uid, name = _wait_start(token, proxy)
    if uid:
        t["users"] = [{"id": uid, "role": "owner", "name": name}] + [u for u in t.get("users", []) if u["id"] != uid]
        ok(f"Вы владелец: {B}{name or uid}{R}")
        try:
            _tg(token, "sendMessage", proxy, chat_id=uid,
                text=f"♊ Готово! Вы владелец бота {APP_NAME}.\nЗапустите бота в терминале — и здесь появится админ-панель.")
        except Exception:
            pass
    else:
        warn(f"Не дождался. Ничего страшного: после запуска напишите боту  {B}/login {t['password']}{R}")


def _wait_start(token, proxy, timeout=180):
    """Ждёт /start от пользователя и возвращает (id, имя). Ctrl+C — пропустить."""
    try:
        _tg(token, "deleteWebhook", proxy)
        last = _tg(token, "getUpdates", proxy, offset=-1, timeout=0).get("result", [])
        offset = last[-1]["update_id"] + 1 if last else 0
    except Exception:
        offset = 0
    deadline = time.time() + timeout
    with Spinner("Жду, пока вы нажмёте Start у бота… (Ctrl+C — пропустить)"):
        try:
            while time.time() < deadline:
                try:
                    res = _tg(token, "getUpdates", proxy, offset=offset, timeout=20, allowed_updates=["message"])
                except Exception:
                    time.sleep(3)
                    continue
                for u in res.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    if (m.get("text") or "").startswith("/start") and m.get("from"):
                        f = m["from"]
                        name = ("@" + f["username"]) if f.get("username") else f.get("first_name", "")
                        _tg(token, "getUpdates", proxy, offset=offset, timeout=0)  # подтвердить получение
                        return int(f["id"]), name
        except KeyboardInterrupt:
            pass
    return None, None


# ---------- мини-апп ----------
def _step_webapp(cfg: Config, n: int):
    w = cfg.data["web"]
    step(n, TOTAL, "📱 Мини-апп и веб-панель",
         "Панель со статистикой и всеми настройками: открывается кнопкой в Telegram-боте или в браузере.")
    c = choose("Как открывать панель?", [
        ("Из Telegram — бот сам получит бесплатный адрес", "пробует Cloudflare, localhost.run и Pinggy, в том числе через прокси бота, и берёт рабочий"),
        ("Из Telegram через свой домен", "если у вас есть сервер с доменом и HTTPS"),
        ("Только в браузере на этом компьютере", "http://127.0.0.1:" + str(w.get("port", 8765))),
        ("Не нужна", "всё управление — кнопками в Telegram-боте"),
    ], default=1)
    w["enabled"] = c != 4
    w["tunnel"] = "auto" if c == 1 else "none"
    if c == 2:
        w["public_url"] = ask_text("Адрес панели (https://…)", w.get("public_url", ""))
    if w["enabled"] and not w.get("password"):
        w["password"] = rand_password(12)
    if w["enabled"]:
        ok("Панель включена" + (" — адрес появится в боте после запуска" if c == 1 else ""))


# ---------- итог ----------
def _step_summary(cfg: Config, n: int):
    step(n, TOTAL, "✅ Готово", "")
    fp, pl = cfg.accounts("funpay"), cfg.accounts("playerok")
    t, w = cfg.data["telegram"], cfg.data["web"]
    owner = next((u for u in t.get("users", []) if u.get("role") == "owner"), None)
    rows = [
        ("FunPay", ", ".join(a.get("name") or a["id"] for a in fp) or f"{D}не подключён{R}"),
        ("Playerok", ", ".join(a.get("name") or a["id"] for a in pl) or f"{D}не подключён{R}"),
        ("Telegram", ("бот подключён" + (f", владелец {owner.get('name') or owner['id']}" if owner else ""))
         if t.get("enabled") and t.get("token") else f"{D}выключен{R}"),
        ("Прокси Telegram", proxy_label(t["proxy"]["list"][0]) if t["proxy"].get("enabled") and t["proxy"].get("list") else "без прокси"),
        ("Панель", {"auto": "Telegram, адрес подберётся сам", "cloudflared": "Telegram + Cloudflare"}.get(
            w.get("tunnel"), w.get("public_url") or "в браузере")
         if w.get("enabled") else f"{D}выключена{R}"),
    ]
    for k, v in rows:
        print(f"  {D}{k:<16}{R} {v}")
    print()
    if w.get("enabled"):
        print(f"  🔐 Пароль веб-панели: {B}{w['password']}{R}  {D}(сохраните — он же в castor_data/config.json){R}")
    if t.get("enabled") and not owner:
        print(f"  🔑 Чтобы стать владельцем, напишите боту: {B}/login {t['password']}{R}")
    print(f"\n  {D}Настройки сохранены в {CONFIG_PATH}{R}")


# ═══════════════════════ прокси, автозапуск, меню ═══════════════════════
def proxy_dialog(cfg: Config):
    while True:
        lst = list(cfg.get("telegram.proxy.list", []))
        rule("🌐 Прокси для Telegram")
        print(f"  Состояние: {GREEN + 'включён' if cfg.get('telegram.proxy.enabled') else D + 'выключен'}{R}")
        for i, p in enumerate(lst, 1):
            print(f"   {B}{i}{R}. {proxy_label(p)}")
        c = choose("Что сделать?", [("Добавить прокси", ""), ("Проверить все", ""), ("Удалить прокси", ""),
                                    ("Выключить прокси" if cfg.get("telegram.proxy.enabled") else "Включить прокси", ""),
                                    ("Назад", "")], default=5)
        if c == 1:
            p, _ = _ask_proxy("для Telegram", "https://api.telegram.org")
            if p:
                cfg.set("telegram.proxy.list", lst + [p])
                cfg.set("telegram.proxy.enabled", True)
        elif c == 2:
            for p in lst + [""]:
                with Spinner(f"Проверяю {proxy_label(p)}…"):
                    good, res = check_proxy(p)
                (ok if good else bad)(f"{proxy_label(p)} — {str(res) + ' мс' if good else res}")
        elif c == 3 and lst:
            i = ask_text("Номер прокси для удаления")
            if i.isdigit() and 1 <= int(i) <= len(lst):
                lst.pop(int(i) - 1)
                cfg.set("telegram.proxy.list", lst)
        elif c == 4:
            cfg.toggle("telegram.proxy.enabled")
        else:
            return


def install_service():
    script = str(BASE_DIR / "main.py")
    if os.name == "nt":
        bat = BASE_DIR / "start_fpcastor.bat"
        bat.write_text(f'@echo off\ncd /d "{BASE_DIR}"\n:loop\n"{sys.executable}" "{script}" --no-dialog\n'
                       f'timeout /t 5\ngoto loop\n', encoding="cp866", errors="ignore")
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        try:
            (startup / "fpcastor.bat").write_text(f'start "" /min "{bat}"\n', encoding="cp866", errors="ignore")
            ok("Бот будет запускаться сам при входе в Windows")
        except Exception as e:
            warn(f"Создан {bat}. Добавьте его в автозагрузку вручную ({e})")
        return
    unit = (f"[Unit]\nDescription={APP_NAME}\nAfter=network-online.target\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={BASE_DIR}\nExecStart={sys.executable} {script} --no-dialog\nRestart=always\n"
            f"RestartSec=5\nEnvironment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=multi-user.target\n")
    path = Path("/etc/systemd/system/fpcastor.service")
    try:
        path.write_text(unit)
        subprocess.run(["systemctl", "daemon-reload"])
        subprocess.run(["systemctl", "enable", "--now", "fpcastor"])
        ok("Сервис установлен. Статус: systemctl status fpcastor · логи: journalctl -u fpcastor -f")
    except PermissionError:
        local = BASE_DIR / "fpcastor.service"
        local.write_text(unit)
        warn(f"Нет прав root. Файл сохранён: {local}\n    sudo cp {local} /etc/systemd/system/ && "
             f"sudo systemctl enable --now fpcastor")


def timed_input(prompt, timeout=10) -> Optional[str]:
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            q.put(input(prompt))
        except Exception:
            q.put(None)
    threading.Thread(target=reader, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def _status_line(cfg: Config):
    def st(v, on="вкл", off="выкл"):
        return f"{GREEN}{on}{R}" if v else f"{D}{off}{R}"
    fp, pl = len(cfg.accounts("funpay")), len(cfg.accounts("playerok"))
    print(f"  🔵 FunPay: {st(fp, f'{fp} акк.', 'нет')}   🟢 Playerok: {st(pl, f'{pl} акк.', 'нет')}   "
          f"🤖 Telegram: {st(cfg.get('telegram.enabled') and cfg.get('telegram.token'))}   "
          f"🌐 Прокси: {st(cfg.get('telegram.proxy.enabled'))}   🧩 Плагинов: {len(list(PLUGINS_DIR.glob('*.py')))}")


def startup_dialog(cfg: Config) -> bool:
    """Возвращает False, если нужно выйти."""
    print(BANNER)
    first = not cfg.exists or not (cfg.accounts("funpay") or cfg.accounts("playerok"))
    if "--setup" in sys.argv or (first and sys.stdin.isatty()):
        if first:
            print(f"  {YEL}Похоже, это первый запуск — давайте настроим бота.{R}")
        wizard(cfg)
        rule()
        if ask_yes("Запускать бота автоматически при включении компьютера?", False):
            install_service()
        return ask_yes("Запустить бота сейчас?", True)
    if not sys.stdin.isatty():
        return True
    _status_line(cfg)
    while True:
        print(f"\n  {B}1{R} ▶ Запустить      {B}2{R} 🧙 Мастер настройки   {B}3{R} 🌐 Прокси Telegram\n"
              f"  {B}4{R} 🤖 Сменить бота   {B}5{R} ⚙ Автозапуск         {B}6{R} 📦 Обновить пакеты   {B}0{R} Выход")
        ch = timed_input(f"  {YEL}›{R} Выбор {D}(Enter — запуск, сам запущу через 10 с){R}: ", 10)
        if ch is None:
            print("\n  ▶ Запускаю…")
            return True
        ch = ch.strip()
        if ch in ("", "1"):
            return True
        if ch == "2":
            wizard(cfg)
        elif ch == "3":
            proxy_dialog(cfg)
        elif ch == "4":
            tok = ask_secret("Токен нового бота от @BotFather", v_tg_token)
            if tok:
                cfg.set("telegram.token", tok)
                cfg.set("telegram.enabled", True)
                ok("Сохранено. После запуска напишите новому боту /start")
        elif ch == "5":
            install_service()
        elif ch == "6":
            for pkg in list(REQUIRED_PACKAGES.values()) + list(PLAYEROK_DEPS.values()):
                with Spinner(f"Обновляю {pkg}…"):
                    good = pip_install(pkg)
                (ok if good else bad)(pkg)
        elif ch == "0":
            return False


# ═══════════════════════ точка входа ═══════════════════════
def run():
    cfg = Config()
    setup_logging(cfg.get("general.log_level", "INFO"))
    if "--install-service" in sys.argv:
        install_service()
        return
    if "--no-dialog" not in sys.argv:
        if not startup_dialog(cfg):
            return
        rule("Запуск")
    elif not cfg.exists:
        cfg.save()
    if cfg.get("web.enabled") and not (cfg.get("web.password") or cfg.get("telegram.password")):
        cfg.set("web.password", rand_password(12))
        log.warning("🔐 Сгенерирован пароль веб-панели: %s", cfg.get("web.password"))
    if cfg.get("telegram.enabled") and cfg.get("telegram.token") and not cfg.get("telegram.password"):
        cfg.set("telegram.password", rand_password())

    from .app import App
    app = App(cfg)
    try:
        app.start()
        print(f"  {GREEN}✔ Бот работает.{R} {D}Остановить — Ctrl+C. Логи — в этом окне и в castor_data/logs{R}\n")
        while app.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Останавливаюсь…")
    finally:
        app.stop()
