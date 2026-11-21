"""Прокси для Telegram: список, проверка, выбор рабочего, автопереключение (failover)."""
from __future__ import annotations

import threading
import time

from .core import Config, check_proxy, diagnose_proxy, log, normalize_proxy, proxy_label


class ProxyManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current = ""          # исходная строка выбранного прокси ("" = напрямую)
        self.results: dict = {}    # proxy -> {"ok", "info", "t"}
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("telegram.proxy.enabled")) and bool(self.list())

    def list(self) -> list:
        return [p for p in self.cfg.get("telegram.proxy.list", []) if str(p).strip()]

    def test(self, p: str) -> tuple:
        d = diagnose_proxy(p)
        ok, info = (True, d["ms"]) if d["ok"] else (False, d["reason"])
        self.results[p] = {"ok": ok, "info": info, "t": time.time(), "variant": d.get("proxy") if d["ok"] else ""}
        return ok, info

    def _use(self, p: str) -> str:
        """Подставляет тот тип подключения (socks5h/socks5/http), который сработал при проверке."""
        r = self.results.get(p) or {}
        return r.get("variant") or p

    def test_all(self) -> list:
        out = []
        for p in self.list():
            ok, info = self.test(p)
            out.append({"proxy": p, "label": proxy_label(p), "ok": ok, "info": info})
        ok, info = check_proxy("")
        out.append({"proxy": "", "label": "напрямую", "ok": ok, "info": info})
        return out

    def choose(self, skip: str = None, check: bool = None) -> str:
        """Возвращает первый рабочий прокси (или "" — напрямую)."""
        check = self.cfg.get("telegram.proxy.check_on_start", True) if check is None else check
        with self.lock:
            if not self.enabled:
                self.current = ""
                return ""
            lst = self.list()
            if skip in lst:  # начинаем со следующего после упавшего
                i = lst.index(skip)
                lst = lst[i + 1:] + lst[:i + 1]
            for p in lst:
                if p == skip and len(lst) > 1:
                    continue
                if not check:
                    self.current = p
                    return p
                ok, info = self.test(p)
                log.info("🌐 Прокси TG %s → %s", proxy_label(p), f"OK {info} мс" if ok else f"нет: {info}")
                if ok:
                    self.current = p
                    return p
            if self.cfg.get("telegram.proxy.allow_direct", True):
                log.warning("🌐 Ни один прокси Telegram не ответил — работаю напрямую")
            self.current = ""
            return ""

    def apihelper_value(self):
        if not self.current:
            return None
        n = normalize_proxy(self._use(self.current))
        return {"https": n, "http": n}

    def label(self) -> str:
        return proxy_label(self.current)
