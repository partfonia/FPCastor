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

from . import APP_NAME, VERSION
from .branding import BANNER_ART
from .bootstrap import PLAYEROK_PIP, REQUIRED_PACKAGES, ensure_playerok_lib, pip_install as _pip_install
from .core import BASE_DIR, CONFIG_PATH, DEFAULT_UA, PLUGINS_DIR, Config, check_proxy, log, proxy_label, rand_password, setup_logging
from .funpay import FunPayClient
from .playerok import PlayerokBridge

C_RESET, C_B, C_P, C_G, C_Y, C_R, C_C = "\033[0m", "\033[1m", "\033[95m", "\033[92m", "\033[93m", "\033[91m", "\033[96m"
if os.name == "nt":
    os.system("")  # включает ANSI в консоли Windows

BANNER = (f"{C_C}{C_B}{BANNER_ART}{C_RESET}  {C_P}♊{C_RESET} {C_C}v{VERSION}{C_RESET}\n"
          f"  {C_B}FunPay{C_RESET} & {C_B}Playerok{C_RESET} Castor · Telegram · мини-апп · плагины Cardinal\n")


def ask(prompt, default="", secret=False, yes_no=False):
    d = f" [{('да' if default else 'нет') if yes_no else ('*скрыто*' if secret and default else default)}]" if default != "" else ""
    while True:
        try:
            v = input(f"{C_Y}?{C_RESET} {prompt}{d}: ").strip()
        except EOFError:
            v = ""
        if yes_no:
            if not v:
                return bool(default)
            if v.lower() in ("y", "yes", "д", "да", "1", "+"):
                return True
            if v.lower() in ("n", "no", "н", "нет", "0", "-"):
                return False
            continue
        return v or default


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


def wizard(cfg: Config):
    print(f"\n{C_B}🧙 Мастер настройки {APP_NAME}{C_RESET} (Enter — оставить как есть)\n")
    # FunPay
    for platform in ("funpay", "playerok"):
        accounts_dialog(cfg, platform)
    # Telegram
    print(f"\n{C_C}━━ Telegram ━━{C_RESET}")
    cfg.data["telegram"]["enabled"] = ask("Включить Telegram-бота (админка, уведомления)?", cfg.get("telegram.enabled"), yes_no=True)
    if cfg.data["telegram"]["enabled"]:
        print(f"  {C_C}Создайте бота у @BotFather и вставьте токен{C_RESET}")
        cfg.data["telegram"]["token"] = ask("Токен бота", cfg.get("telegram.token"), secret=True)
        pwd = cfg.get("telegram.password") or rand_password()
        cfg.data["telegram"]["password"] = ask("Пароль для входа в бота (/login ПАРОЛЬ)", pwd)
        owners = [u for u in cfg.get("telegram.users", []) if u.get("role") == "owner"]
        ids = ask("Ваш Telegram ID — вы станете владельцем (можно пропустить и войти через /login)",
                  ",".join(str(u["id"]) for u in owners))
        others = [u for u in cfg.get("telegram.users", []) if u.get("role") != "owner"]
        cfg.data["telegram"]["users"] = [{"id": int(x), "role": "owner", "name": ""}
                                         for x in re.findall(r"\d+", str(ids))] + others
        if ask("Нужен прокси для Telegram (если api.telegram.org заблокирован)?",
               cfg.get("telegram.proxy.enabled"), yes_no=True):
            lst = list(cfg.get("telegram.proxy.list", []))
            print("  Формат: socks5://user:pass@ip:port | http://ip:port | ip:port:user:pass. Пустая строка — конец.")
            while True:
                p = ask("  Прокси")
                if not p:
                    break
                ok, info = check_proxy(p)
                print(f"   {C_G + '✔ ' + str(info) + ' мс' if ok else C_R + '✘ ' + str(info)}{C_RESET}")
                lst.append(p)
            cfg.data["telegram"]["proxy"]["list"] = lst
            cfg.data["telegram"]["proxy"]["enabled"] = bool(lst)
        else:
            cfg.data["telegram"]["proxy"]["enabled"] = False
    # Web
    print(f"\n{C_Y}━━ Веб-панель / Мини-апп ━━{C_RESET}")
    cfg.data["web"]["enabled"] = ask("Включить веб-панель и мини-апп?", cfg.get("web.enabled"), yes_no=True)
    if cfg.data["web"]["enabled"]:
        cfg.data["web"]["port"] = int(ask("Порт", str(cfg.get("web.port"))) or 8765)
        cfg.data["web"]["password"] = ask("Пароль веб-панели", cfg.get("web.password") or cfg.get("telegram.password") or rand_password())
        print(f"  {C_C}Мини-аппу Telegram нужен HTTPS. Варианты: cloudflared (бесплатно, автоматически) или свой домен.{C_RESET}")
        if ask("Поднять бесплатный HTTPS-туннель cloudflared?", cfg.get("web.tunnel") == "cloudflared", yes_no=True):
            cfg.data["web"]["tunnel"] = "cloudflared"
        else:
            cfg.data["web"]["tunnel"] = "none"
            cfg.data["web"]["public_url"] = ask("Свой HTTPS-адрес панели (Enter — нет)", cfg.get("web.public_url"))
    cfg.save()
    print(f"\n{C_G}✔ Настройки сохранены в {CONFIG_PATH}{C_RESET}")
    if cfg.get("telegram.enabled") and not cfg.get("telegram.users"):
        print(f"  Напишите боту: {C_B}/login {cfg.get('telegram.password')}{C_RESET}")


def accounts_dialog(cfg: Config, platform: str):
    title = "FunPay" if platform == "funpay" else "Playerok"
    color = C_P if platform == "funpay" else C_G
    print(f"\n{color}━━ {title} ━━{C_RESET}")
    accs = cfg.accounts(platform)
    for a in accs:
        print(f"   • {a['id']} {a.get('name') or ''} {'(выкл)' if not a.get('enabled', True) else ''}")
    if not ask(f"Добавить аккаунт {title}?" if accs else f"Подключить {title}?", not accs and platform == "funpay",
               yes_no=True):
        return
    if platform == "playerok" and not ensure_playerok_lib():
        print(f"  {C_R}Без библиотеки playerokapi Playerok работать не будет{C_RESET}")
    while True:
        a = {"id": cfg.new_account_id(platform), "name": "", "enabled": True, "proxy": "", "user_agent": "",
             "auto_delivery": True}
        if platform == "funpay":
            print(f"  {C_C}golden_key: funpay.com → F12 → Application → Cookies → golden_key{C_RESET}")
            a["golden_key"] = ask("golden_key", secret=True)
            a["auto_raise"] = True
        else:
            print(f"  {C_C}token: playerok.com → F12 → Application → Cookies → token{C_RESET}")
            a["token"], a["cookies"] = ask("token", secret=True), ""
        if not (a.get("golden_key") or a.get("token")):
            return
        a["name"] = ask("Название аккаунта (для себя)", f"{title} {a['id'][2:]}")
        a["proxy"] = ask("Прокси для аккаунта (Enter — без)")
        try:
            if platform == "funpay":
                c = FunPayClient(a["golden_key"], DEFAULT_UA, a["proxy"]).login()
                print(f"  {C_G}✔ Вход: {c.username}, баланс {c.balance} {c.currency}{C_RESET}")
            else:
                b = PlayerokBridge(cfg, a).connect()
                print(f"  {C_G}✔ Вход: {b.username}{C_RESET}")
        except Exception as e:
            print(f"  {C_R}✘ Не удалось войти: {e}. Аккаунт сохранён — проверьте ключ позже.{C_RESET}")
        accs.append(a)
        cfg.data["accounts"][platform] = accs
        cfg.data[platform]["enabled"] = True
        if not ask("Добавить ещё один аккаунт?", False, yes_no=True):
            return


def proxy_dialog(cfg: Config):
    while True:
        lst = list(cfg.get("telegram.proxy.list", []))
        print(f"\n  Прокси Telegram: {'вкл' if cfg.get('telegram.proxy.enabled') else 'выкл'}")
        for i, p in enumerate(lst):
            print(f"   {i + 1}. {proxy_label(p)}")
        print("  [a] добавить  [d N] удалить  [t] проверить  [on]/[off] вкл/выкл  [Enter] назад")
        c = ask("  Действие").strip().lower()
        if not c:
            return
        if c == "a":
            p = ask("  Прокси (socks5://user:pass@ip:port | ip:port:user:pass)")
            if p:
                ok, info = check_proxy(p)
                print(f"   {'✔ ' + str(info) + ' мс' if ok else '✘ ' + str(info)}")
                lst.append(p)
                cfg.set("telegram.proxy.list", lst)
                cfg.set("telegram.proxy.enabled", True)
        elif c.startswith("d") and re.findall(r"\d+", c):
            i = int(re.findall(r"\d+", c)[0]) - 1
            if 0 <= i < len(lst):
                lst.pop(i)
                cfg.set("telegram.proxy.list", lst)
        elif c == "t":
            for p in lst + [""]:
                ok, info = check_proxy(p)
                print(f"   {'✔' if ok else '✘'} {proxy_label(p)} — {info}")
        elif c in ("on", "off"):
            cfg.set("telegram.proxy.enabled", c == "on")


def install_service():
    script = str(BASE_DIR / "main.py")
    if os.name == "nt":
        bat = BASE_DIR / "start_fpcastor.bat"
        bat.write_text(f'@echo off\ncd /d "{BASE_DIR}"\n:loop\n"{sys.executable}" "{script}" --no-dialog\n'
                       f'timeout /t 5\ngoto loop\n', encoding="cp866", errors="ignore")
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        try:
            (startup / "fpcastor.bat").write_text(f'start "" /min "{bat}"\n', encoding="cp866", errors="ignore")
            print(f"{C_G}✔ Создан {bat} и добавлен в автозагрузку Windows{C_RESET}")
        except Exception as e:
            print(f"{C_Y}Создан {bat}. Добавьте его в автозагрузку вручную ({e}){C_RESET}")
        return
    unit = (f"[Unit]\nDescription={APP_NAME}\nAfter=network-online.target\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={BASE_DIR}\nExecStart={sys.executable} {script} --no-dialog\nRestart=always\n"
            f"RestartSec=5\nEnvironment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=multi-user.target\n")
    path = Path("/etc/systemd/system/fpcastor.service")
    try:
        path.write_text(unit)
        subprocess.run(["systemctl", "daemon-reload"])
        subprocess.run(["systemctl", "enable", "--now", "fpcastor"])
        print(f"{C_G}✔ Сервис установлен: systemctl status fpcastor · journalctl -u fpcastor -f{C_RESET}")
    except PermissionError:
        local = BASE_DIR / "fpcastor.service"
        local.write_text(unit)
        print(f"{C_Y}Нет прав root. Файл сохранён: {local}\n  sudo cp {local} /etc/systemd/system/ && "
              f"sudo systemctl enable --now fpcastor{C_RESET}")


def startup_dialog(cfg: Config) -> bool:
    """Возвращает False, если нужно выйти."""
    print(BANNER)
    first = not cfg.exists or not (cfg.accounts("funpay") or cfg.accounts("playerok"))
    if "--setup" in sys.argv or (first and sys.stdin.isatty()):
        if first:
            print(f"{C_Y}Похоже, это первый запуск — давайте настроим бота.{C_RESET}")
        wizard(cfg)
        if ask("Установить автозапуск (сервис)?", False, yes_no=True):
            install_service()
        return ask("Запустить бота сейчас?", True, yes_no=True)
    if not sys.stdin.isatty():
        return True
    st = lambda v: f"{C_G}вкл{C_RESET}" if v else f"{C_R}выкл{C_RESET}"  # noqa
    print(f"  FunPay: {len(cfg.accounts('funpay'))} акк.   Playerok: {len(cfg.accounts('playerok'))} акк.   "
          f"Telegram: {st(cfg.get('telegram.enabled') and cfg.get('telegram.token'))}   "
          f"Прокси TG: {st(cfg.get('telegram.proxy.enabled'))}   Плагинов: {len(list(PLUGINS_DIR.glob('*.py')))}\n")
    while True:
        print(f"  {C_B}[1]{C_RESET} ▶ Запуск   {C_B}[2]{C_RESET} 🧙 Мастер настройки   {C_B}[3]{C_RESET} 🌐 Проверить прокси TG\n"
              f"  {C_B}[4]{C_RESET} ⚙ Автозапуск (сервис)   {C_B}[5]{C_RESET} 📦 Обновить зависимости\n"
              f"  {C_B}[6]{C_RESET} 🤖 Сменить Telegram-бота   {C_B}[7]{C_RESET} 🌐 Прокси Telegram   {C_B}[0]{C_RESET} Выход")
        ch = timed_input(f"{C_Y}Выбор (автозапуск через 10 с): {C_RESET}", 10)
        if ch is None:
            print("\n▶ Запускаю…")
            return True
        ch = ch.strip()
        if ch in ("", "1"):
            return True
        if ch == "2":
            wizard(cfg)
        elif ch == "3":
            lst = cfg.get("telegram.proxy.list", [])
            if not lst:
                print("  Список прокси пуст")
            for p in lst:
                ok, info = check_proxy(p)
                print(f"  {'✔' if ok else '✘'} {p.split('@')[-1]} — {info}")
            ok, info = check_proxy("")
            print(f"  {'✔' if ok else '✘'} напрямую — {info}")
        elif ch == "4":
            install_service()
        elif ch == "5":
            for pkg in REQUIRED_PACKAGES.values():
                print(f"  {'✔' if _pip_install(pkg) else '✘'} {pkg}")
            if cfg.get("playerok.enabled"):
                for s in PLAYEROK_PIP[:1]:
                    print(f"  {'✔' if _pip_install(s) else '✘'} {s}")
        elif ch == "6":
            tok = ask("Новый токен бота (@BotFather)", secret=True)
            if tok:
                cfg.set("telegram.token", tok.strip())
                cfg.set("telegram.enabled", True)
                print(f"  {C_G}✔ Сохранено. После запуска напишите новому боту /start{C_RESET}")
        elif ch == "7":
            proxy_dialog(cfg)
        elif ch == "0":
            return False


# ════════════════════════════════════════════════════════════════════
# 15. ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════
def run():
    cfg = Config()
    setup_logging(cfg.get("general.log_level", "INFO"))
    if "--install-service" in sys.argv:
        install_service()
        return
    if "--no-dialog" not in sys.argv:
        if not startup_dialog(cfg):
            return
    elif not cfg.exists:
        cfg.save()
    if cfg.get("web.enabled") and not (cfg.get("web.password") or cfg.get("telegram.password")):
        cfg.set("web.password", rand_password())
        log.warning("🔐 Сгенерирован пароль веб-панели: %s", cfg.get("web.password"))
    if cfg.get("telegram.enabled") and cfg.get("telegram.token") and not cfg.get("telegram.password"):
        cfg.set("telegram.password", rand_password())

    from .app import App
    app = App(cfg)
    try:
        app.start()
        while app.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nОстанавливаюсь…")
    finally:
        app.stop()


