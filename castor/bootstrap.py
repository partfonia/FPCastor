"""Автоустановка зависимостей: понятные сообщения, прогресс и журнал ошибок pip."""
import importlib
import importlib.util
import itertools
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "castor", "vendor")
INSTALL_LOG = os.path.join(ROOT, "castor_data", "logs", "install.log")

# модуль : пакет pip
REQUIRED_PACKAGES = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "telebot": "pyTelegramBotAPI",
    "socks": "PySocks",
}
# библиотека playerokapi встроена в castor/vendor — нужны только её зависимости
PLAYEROK_DEPS = {
    "curl_cffi": "curl_cffi",
    "tls_requests": "wrapper-tls-requests",
    "websocket": "websocket-client",
}
PLAYEROK_MIN_PY = (3, 11)

if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

if os.name == "nt":
    os.system("")  # ANSI-цвета в консоли Windows

_G, _R, _Y, _D, _0 = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"
last_error = ""


class Spinner:
    """Крутилка «⠋ Устанавливаю…» на время долгой операции."""

    def __init__(self, text):
        self.text, self._stop = text, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r  {ch} {self.text}")
            sys.stdout.flush()
            time.sleep(0.1)

    def __enter__(self):
        if sys.stdout.isatty():
            self._t.start()
        else:
            print(f"  … {self.text}")
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._t.is_alive():
            self._t.join()
            sys.stdout.write("\r" + " " * (len(self.text) + 6) + "\r")
            sys.stdout.flush()


def _log(text):
    try:
        os.makedirs(os.path.dirname(INSTALL_LOG), exist_ok=True)
        with open(INSTALL_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{text}\n")
    except Exception:
        pass


def _explain(output: str) -> str:
    o = output.lower()
    if any(x in o for x in ("connection", "timed out", "proxy", "ssl", "could not fetch", "network", "getaddrinfo")):
        return "нет доступа к pypi.org — проверьте интернет или VPN и запустите ещё раз"
    if "no matching distribution" in o or "could not find a version" in o:
        return "pip не нашёл пакет для вашей версии Python — обновите Python до 3.12 с python.org"
    if "permission" in o or "access is denied" in o or "отказано в доступе" in o:
        return "нет прав на запись — запустите install.bat от имени администратора или используйте venv"
    if "microsoft visual c++" in o or "build wheels" in o or "error: command" in o:
        return "пакет пытается собраться из исходников — обновите Python до 3.12 и pip (python -m pip install -U pip)"
    return "подробности в файле castor_data/logs/install.log"


def pip_install(spec: str) -> bool:
    """Ставит пакет; при ошибке сохраняет вывод pip в журнал и понятную причину в last_error."""
    global last_error
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", spec]
    output = ""
    for extra in ([], ["--break-system-packages"], ["--user", "--break-system-packages"]):
        try:
            r = subprocess.run(cmd + extra, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace")
        except Exception as e:
            output = str(e)
            break
        output = r.stdout
        if r.returncode == 0:
            importlib.invalidate_caches()
            return True
        if "externally-managed" not in output and "--break-system-packages" not in output:
            break
    _log(f"pip install {spec}\n{output}")
    last_error = _explain(output)
    return False


def ensure_pip():
    try:
        import pip  # noqa: F401
    except ImportError:
        with Spinner("Устанавливаю pip…"):
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True)


def _install(packages: dict, title: str) -> list:
    """→ список пакетов, которые не удалось поставить."""
    missing = [(m, p) for m, p in packages.items() if importlib.util.find_spec(m) is None]
    if not missing:
        return []
    ensure_pip()
    print(f"\n  📦 {title}: {', '.join(p for _, p in missing)}")
    failed = []
    for i, (mod, pkg) in enumerate(missing):
        with Spinner(f"Устанавливаю {pkg}…"):
            ok = pip_install(pkg)
        print(f"  {_G + '✔' if ok else _R + '✘'} {pkg}{_0}" + ("" if ok else f"  {_D}— {last_error}{_0}"))
        if not ok:
            failed.append(pkg)
            if "pypi.org" in last_error:  # нет сети — остальные пакеты тоже не скачаются, не ждём зря
                failed += [p for _, p in missing[i + 1:]]
                break
    return failed


def ensure_dependencies():
    failed = _install(REQUIRED_PACKAGES, "Устанавливаю нужные пакеты")
    hard = [p for p in failed if p != "PySocks"]
    if hard:
        print(f"\n  {_R}Без этих пакетов бот не запустится.{_0} Установите вручную:\n"
              f"    {sys.executable} -m pip install -r requirements.txt\n")
        sys.exit(1)


def playerok_status() -> tuple:
    """(готово, причина). Библиотека встроена — проверяем версию Python и зависимости."""
    if sys.version_info < PLAYEROK_MIN_PY:
        v = ".".join(map(str, sys.version_info[:3]))
        return False, (f"для Playerok нужен Python 3.11 или новее, а у вас {v}. "
                       f"Скачайте Python 3.12 с python.org и запустите install.bat заново")
    failed = _install(PLAYEROK_DEPS, "Ставлю компоненты для Playerok")
    if failed:
        return False, f"не установились {', '.join(failed)}: {last_error}"
    try:
        importlib.invalidate_caches()
        importlib.import_module("playerokapi.account")
        importlib.import_module("playerokapi.listener.listener")
    except Exception as e:
        _log(f"import playerokapi: {e!r}")
        return False, f"библиотека Playerok не загрузилась: {e}"
    return True, ""


def ensure_playerok_lib() -> bool:
    ok, why = playerok_status()
    if not ok:
        print(f"  {_R}✘ Playerok недоступен:{_0} {why}")
    return ok
