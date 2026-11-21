"""Автоустановка зависимостей (pip) без лишних действий со стороны пользователя."""
import importlib
import importlib.util
import subprocess
import sys

REQUIRED_PACKAGES = {
    # модуль : пакет pip
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "telebot": "pyTelegramBotAPI",
    "socks": "PySocks",  # socks5-прокси
}
PLAYEROK_PIP = ["playerokapi", "git+https://github.com/alleexxeeyy/PlayerokAPI.git"]


def pip_install(spec: str) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", spec]
    for extra in ([], ["--break-system-packages"], ["--user", "--break-system-packages"]):
        try:
            r = subprocess.run(cmd + extra, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if r.returncode == 0:
                importlib.invalidate_caches()
                return True
        except Exception as e:  # noqa
            print("  pip:", e)
            return False
    return False


def ensure_pip():
    try:
        import pip  # noqa: F401
    except ImportError:
        print("[setup] pip не найден, ставлю через ensurepip…")
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"])


def missing_required():
    return [(m, p) for m, p in REQUIRED_PACKAGES.items() if importlib.util.find_spec(m) is None]


def ensure_dependencies():
    missing = missing_required()
    if not missing:
        return
    ensure_pip()
    print(f"[setup] Устанавливаю зависимости: {', '.join(p for _, p in missing)}")
    for mod, pkg in missing:
        ok = pip_install(pkg)
        print(f"  {'✔' if ok else '✘'} {pkg}")
        if not ok and mod != "socks":
            print(f"\n[setup] Не удалось установить {pkg}. Установите вручную:\n"
                  f"  {sys.executable} -m pip install -r requirements.txt\n")
            sys.exit(1)


def ensure_playerok_lib() -> bool:
    if importlib.util.find_spec("playerokapi") is not None:
        return True
    if sys.version_info < (3, 11):
        print("[setup] Для Playerok нужен Python 3.11+")
        return False
    ensure_pip()
    print("[setup] Устанавливаю библиотеку Playerok (playerokapi)…")
    for spec in PLAYEROK_PIP:
        if pip_install(spec) and importlib.util.find_spec("playerokapi") is not None:
            print("  ✔ playerokapi")
            return True
    print("  ✘ playerokapi не установилась (для git-варианта нужен git)")
    return False
