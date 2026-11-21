"""Бесплатный HTTPS-адрес для мини-аппа: несколько способов и автоматический выбор рабочего.

Способы (по порядку в режиме «авто»):
  1. Cloudflare (cloudflared, протокол http2 — работает там, где UDP/QUIC режут)
  2. localhost.run по ssh — напрямую
  3. localhost.run по ssh через прокси Telegram-бота (если Cloudflare и прямой ssh недоступны)
  4. Pinggy по ssh на порт 443

Каждый адрес проверяется запросом /api/health снаружи (через прокси бота и напрямую). Кнопку «Панель»
бот ставит только на проверенный адрес. Раз в 2 минуты адрес перепроверяется; если умер — берётся следующий способ.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

import requests

from .core import BIN_DIR, log, proxy_label, proxy_url

if TYPE_CHECKING:
    from .app import App

MODES = {
    "auto": "Авто — бот сам подберёт рабочий",
    "cloudflared": "Cloudflare",
    "localhostrun": "localhost.run",
    "pinggy": "Pinggy",
    "none": "Выключен (свой адрес или только браузер)",
}
PROVIDER_NAMES = {"cloudflared": "Cloudflare", "localhostrun": "localhost.run",
                  "localhostrun_proxy": "localhost.run через прокси", "pinggy": "Pinggy"}
URL_RX = {
    "cloudflared": re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
    "localhostrun": re.compile(r"https://[a-z0-9-]+\.(?:lhr\.life|lhr\.pro|localhost\.run)"),
    "pinggy": re.compile(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.pinggy\.(?:link|online|io)"),
}
NOWINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _humanize(provider: str, line: str) -> str:
    low = line.lower()
    if "failed to dial" in low or "edge" in low and ("timeout" in low or "unreachable" in low):
        return "Cloudflare недоступен с этого компьютера (провайдер режет соединение)"
    if "context deadline exceeded" in low or "i/o timeout" in low:
        return "сервер туннеля не отвечает"
    if "permission denied" in low:
        return "сервер отклонил вход по ssh"
    if "connection refused" in low or "connection reset" in low or "connection closed" in low:
        return "сервер туннеля закрыл соединение"
    if "could not resolve" in low or "no such host" in low:
        return "не удаётся найти адрес сервера — нет интернета или DNS"
    if "netpipe:" in low:
        return "прокси: " + line.split("netpipe:", 1)[1].strip()
    if "429" in low or "too many" in low:
        return "сервис временно ограничил запросы — попробую позже"
    return line.strip()[-160:]


class _ProxyForward:
    """Локальный порт, который пересылает соединения на host:port через прокси бота.
    Нужен, чтобы ssh шёл через прокси без ProxyCommand (там проблемы с путями на Windows)."""

    def __init__(self, proxy: str, host: str, port: int):
        from .netpipe import open_via_proxy
        self._open = open_via_proxy
        self.proxy, self.host, self.port = proxy, host, port
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.local_port = self.srv.getsockname()[1]
        self.error = ""
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            r = self._open(self.proxy, self.host, self.port)
        except Exception as e:
            self.error = str(e)
            c.close()
            return

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except Exception:
                pass
            for x in (a, b):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
        threading.Thread(target=pump, args=(c, r), daemon=True).start()
        pump(r, c)

    def close(self):
        try:
            self.srv.close()
        except Exception:
            pass


class TunnelManager:
    def __init__(self, app: "App", port: int):
        self.app, self.port = app, port
        self.proc: Optional[subprocess.Popen] = None
        self.fwd: Optional[_ProxyForward] = None
        self.info = {"state": "off", "provider": "", "error": "", "checked": 0, "url": ""}
        self.attempts: deque = deque(maxlen=8)   # (время, способ, результат)
        self._skip = threading.Event()
        self._reset = threading.Event()
        self._thread = None

    # ─────────── публичное ───────────
    @property
    def mode(self) -> str:
        m = self.app.cfg.get("web.tunnel", "none")
        return "auto" if m in ("", None) else m

    def start(self):
        if self.mode == "none":
            self.info.update(state="off")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="tunnel", daemon=True)
        self._thread.start()

    def next(self):
        """Бросить текущий способ и попробовать следующий."""
        self.info.update(state="starting", error="")
        self._skip.set()
        self._kill()

    def reset(self):
        """Способ поменяли в настройках — начать заново с первого способа нового режима."""
        self._reset.set()
        if self._thread and self._thread.is_alive():
            self.next()
        else:
            self.start()

    def stop(self):
        self._kill()

    def check(self, url: str = None) -> bool:
        url = url or self.app.public_url
        if not url.startswith("https://"):
            self.info.update(state="off" if not url else "failed", error="нет HTTPS-адреса")
            return False
        ok, err = self._reachable(url)
        self.info.update(state="ok" if ok else "unreachable", error="" if ok else err, checked=time.time())
        return ok

    # ─────────── проверка снаружи ───────────
    def _reachable(self, url: str) -> tuple:
        """Открывается ли адрес: через прокси бота (так ближе к телефону с VPN) и напрямую."""
        routes = []
        px = self.app.proxy.current if getattr(self.app, "proxy", None) else ""
        if px:
            routes.append(("через прокси", {"http": proxy_url(px), "https": proxy_url(px)}))
        routes.append(("напрямую", None))
        err = ""
        for name, proxies in routes:
            try:
                r = requests.get(url + "/api/health", timeout=12, proxies=proxies)
                if r.ok and r.json().get("ok"):
                    return True, ""
                err = f"сайт ответил {r.status_code}"
            except requests.Timeout:
                err = "не ответил за 12 с"
            except Exception:
                err = "не удалось подключиться"
        return False, err

    # ─────────── основной цикл ───────────
    def _order(self) -> list:
        m = self.mode
        px = self.app.proxy.current if getattr(self.app, "proxy", None) else ""
        if m == "auto":
            order = ["cloudflared", "localhostrun"] + (["localhostrun_proxy"] if px else []) + ["pinggy"]
        elif m == "localhostrun":
            order = ["localhostrun"] + (["localhostrun_proxy"] if px else [])
        else:
            order = [m]
        return order

    def _loop(self):
        fails_in_row = 0
        while self.app.running and self.mode != "none":
            order = self._order()
            self._reset.clear()
            for prov in order:
                if not self.app.running or self.mode == "none":
                    self.info.update(state="off", provider="")
                    return
                if self._reset.is_set():
                    break
                self._skip.clear()
                ok = self._run_provider(prov)
                if ok:
                    fails_in_row = 0
            if self._reset.is_set():
                continue
            fails_in_row += 1
            wait = min(300, 20 * fails_in_row)
            self.info.update(state="failed", error=self.info.get("error") or "ни один способ не сработал")
            log.warning("📱 Мини-апп: ни один способ туннеля не сработал, повтор через %s с", wait)
            for _ in range(wait):
                if self._skip.is_set() or not self.app.running:
                    break
                time.sleep(1)

    def _run_provider(self, prov: str) -> bool:
        """Запускает способ, ждёт адрес, проверяет и держит, пока жив. → был ли он рабочим."""
        name = PROVIDER_NAMES.get(prov, prov)
        if self.info.get("url") and self.app.public_url == self.info["url"]:
            self.app.public_url = ""  # старый адрес туннеля уже не работает — не показываем его
        self.info.update(state="starting", provider=prov, error="", url="")
        log.info("📱 Мини-апп: пробую %s…", name)
        try:
            url, err = self._launch(prov)
        except Exception as e:
            url, err = "", str(e)
        if not url:
            self._fail(prov, err or "не выдал адрес")
            return False
        # адрес получен — ждём, когда он реально откроется (Cloudflare включает до минуты)
        ok = False
        for _ in range(10):
            if self._skip.is_set() or not self._alive():
                break
            ok, err = self._reachable(url)
            if ok:
                break
            time.sleep(6)
        if not ok:
            self._fail(prov, err or "адрес не открывается")
            return False
        self.info.update(state="ok", url=url, error="", checked=time.time())
        self.attempts.append((time.time(), name, "работает"))
        if url != self.app.public_url:
            self.app.public_url = url
            self._announce(url, name)
        # держим, пока работает
        bad = 0
        while self.app.running and not self._skip.is_set() and self._alive():
            for _ in range(120):
                if self._skip.is_set() or not self._alive() or not self.app.running:
                    break
                time.sleep(1)
            if self._skip.is_set() or not self._alive():
                break
            ok, err = self._reachable(url)
            self.info.update(checked=time.time())
            if ok:
                bad = 0
                self.info.update(state="ok", error="")
            else:
                bad += 1
                self.info.update(state="unreachable", error=err)
                if bad >= 3:
                    log.warning("📱 Мини-апп: %s перестал открываться (%s) — переключаюсь", name, err)
                    break
        self._kill()
        if self.app.running and not self._skip.is_set():
            self.attempts.append((time.time(), name, "оборвался"))
        return True

    def _fail(self, prov, err):
        name = PROVIDER_NAMES.get(prov, prov)
        err = _humanize(prov, err) if err else "не сработал"
        self.info.update(state="failed", error=f"{name}: {err}")
        self.attempts.append((time.time(), name, err))
        log.warning("📱 Мини-апп: %s не подошёл — %s", name, err)
        self._kill()

    def _alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _kill(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if self.fwd:
            self.fwd.close()
            self.fwd = None

    # ─────────── запуск способов ───────────
    def _read_url(self, rx, timeout: float, ready_marker: str = None) -> tuple:
        """Читает вывод процесса, пока не появится адрес (и маркер готовности, если задан)."""
        found, ready, last_err = "", ready_marker is None, ""
        lines = deque(maxlen=40)
        done = threading.Event()

        def reader():
            nonlocal found, ready, last_err
            for line in self.proc.stdout:
                lines.append(line)
                if not found:
                    m = rx.search(line)
                    if m:
                        found = m.group(0)
                if ready_marker and ready_marker in line:
                    ready = True
                if re.search(r"\b(ERR|error|failed|denied|refused)\b", line, re.I):
                    last_err = line
                if found and ready:
                    done.set()
            done.set()
        threading.Thread(target=reader, daemon=True).start()
        done.wait(timeout)
        if found and not ready:  # адрес есть, но соединения с сервером нет
            return "", last_err or "нет соединения с сервером туннеля"
        if not found:
            if not self._alive():
                return "", last_err or (lines[-1] if lines else "процесс завершился")
            return "", last_err or "не выдал адрес за отведённое время"
        return found, ""

    def _popen(self, args, env=None):
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", env=env, **NOWINDOW)

    def _launch(self, prov: str) -> tuple:
        local = f"http://127.0.0.1:{self.port}"
        if prov == "cloudflared":
            binp = self._cloudflared_bin()
            if not binp:
                return "", "cloudflared не скачался — проверьте интернет"
            env = dict(os.environ)
            px = self.app.proxy.current if getattr(self.app, "proxy", None) else ""
            if px:  # запрос адреса у Cloudflare — через прокси бота (Go понимает socks5:// и http://)
                env["HTTPS_PROXY"] = proxy_url(px).replace("socks5h://", "socks5://")
            self._popen([binp, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", local], env)
            return self._read_url(URL_RX["cloudflared"], 60, ready_marker="Registered tunnel connection")
        ssh = self._ssh_bin()
        if not ssh:
            return "", "нет программы ssh (в Windows 10/11 она встроена: Параметры → Приложения → Доп. компоненты → «Клиент OpenSSH»)"
        base = [ssh, "-T", "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR"]
        if prov in ("localhostrun", "localhostrun_proxy"):
            host, port = "localhost.run", 22
            if prov == "localhostrun_proxy":
                px = proxy_url(self.app.proxy.current)
                self.fwd = _ProxyForward(px, host, port)
                base += ["-o", "HostKeyAlias=localhost.run", "-p", str(self.fwd.local_port)]
                target = "nokey@127.0.0.1"
                log.info("📱 Мини-апп: ssh через прокси %s", proxy_label(self.app.proxy.current))
            else:
                target = f"nokey@{host}"
            self._popen(base + ["-R", f"80:127.0.0.1:{self.port}", target])
            url, err = self._read_url(URL_RX["localhostrun"], 45)
            if not url and self.fwd and self.fwd.error:
                err = "прокси: " + self.fwd.error
            return url, err
        if prov == "pinggy":
            self._popen(base + ["-p", "443", "-R", f"0:127.0.0.1:{self.port}", "a.pinggy.io"])
            try:  # Pinggy может спросить пароль — для бесплатного режима он пустой
                self.proc.stdin.write("\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            return self._read_url(URL_RX["pinggy"], 45)
        return "", "неизвестный способ"

    def _ssh_bin(self) -> Optional[str]:
        found = shutil.which("ssh")
        if found:
            return found
        if os.name == "nt":
            for base in (os.environ.get("SystemRoot", r"C:\Windows"),):
                for sub in ("System32", "Sysnative"):
                    p = os.path.join(base, sub, "OpenSSH", "ssh.exe")
                    if os.path.exists(p):
                        return p
        return None

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
            return None
        dst = BIN_DIR / fname
        if not dst.exists():
            log.info("⬇️ Скачиваю cloudflared…")
            px = self.app.proxy.current if getattr(self.app, "proxy", None) else ""
            for proxies in ([None] + ([{"http": proxy_url(px), "https": proxy_url(px)}] if px else [])):
                try:
                    with requests.get(url, stream=True, timeout=60, proxies=proxies) as r:
                        r.raise_for_status()
                        with open(dst, "wb") as f:
                            for chunk in r.iter_content(1 << 16):
                                f.write(chunk)
                    dst.chmod(0o755)
                    break
                except Exception as e:
                    log.warning("cloudflared не скачался%s: %s", " через прокси" if proxies else "", e)
                    dst.unlink(missing_ok=True)
            if not dst.exists():
                return None
        return str(dst)

    # ─────────── сообщение владельцу ───────────
    def _announce(self, url: str, name: str):
        log.info("📱 Мини-апп работает через %s: %s", name, url)
        tg = self.app.tg
        if tg:
            try:
                tg.setup_menu_button()
            except Exception:
                pass
        self.app.notify("start", f"📱 <b>Приложение готово</b> ({name}).\nОткрывайте кнопкой «📱 Панель» слева от поля "
                                 f"ввода или в /menu.\n<i>Адрес меняется при перезапуске — старые кнопки не работают.</i>",
                        buttons=[[("📱 Открыть приложение", ("webapp", url))]])
