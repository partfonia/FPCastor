"""Оформление: подпись в сообщениях покупателям, описание Telegram-бота, ссылки проекта."""
from __future__ import annotations

import hashlib

from . import APP_NAME, SIGN, VERSION

DEFAULT_WATERMARK = "♊ 𝑪𝒂𝒔𝒕𝒐𝒓"
WATERMARK_PRESETS = [
    "♊ 𝑪𝒂𝒔𝒕𝒐𝒓",
    "𝑪𝒂𝒔𝒕𝒐𝒓 ♊",
    "✦ 𝓒𝓪𝓼𝓽𝓸𝓻 ✦",
    "♊︎ 𝘾𝙖𝙨𝙩𝙤𝙧",
    "[♊]",
]
TITLE_FANCY = "𝑭𝒖𝒏𝑷𝒂𝒚 & 𝑷𝒍𝒂𝒚𝒆𝒓𝒐𝒌 𝑪𝒂𝒔𝒕𝒐𝒓"

LINK_NAMES = {
    "updates": ("🔄", "Обновления"),
    "plugins": ("🧩", "Плагины"),
    "chat": ("💬", "Чат"),
    "donate": ("💰", "Донат"),
    "github": ("🛠", "Создать своего бота"),
}

FEATURES = [
    "🔵 FunPay + 🟢 Playerok в одном боте",
    "👥 Несколько аккаунтов",
    "🤖 Автовыдача и API поставщиков",
    "🚀 Автоподнятие лотов",
    "📭 Скрытие лотов без товара",
    "💬 Автоответы и шаблоны",
    "📈 Учёт прибыли",
    "💸 Автовывод средств",
    "👨‍💼 Роли помощников",
    "📱 Мини-апп и плагины",
]


# ---------- подпись ----------
def watermark(cfg, manual: bool = False) -> str:
    """Текущая подпись или "" если выключена (для ручных ответов — своя настройка)."""
    g = cfg.get("general", {})
    if not g.get("watermark_enabled", True):
        return ""
    if manual and not g.get("watermark_manual", True):
        return ""
    return (g.get("watermark") or DEFAULT_WATERMARK).strip()


def apply_watermark(cfg, text: str, manual: bool = False) -> str:
    wm = watermark(cfg, manual)
    if not wm or not text or text.startswith(wm):
        return text
    sep = "\n" if cfg.get("general.watermark_newline", True) else " "
    return f"{wm}{sep}{text}"


def is_bot_message(cfg, text: str) -> bool:
    """Сообщение отправлено ботом (начинается с подписи) — как by_bot у Кардинала."""
    wm = (cfg.get("general.watermark") or DEFAULT_WATERMARK).strip()
    return bool(text) and bool(wm) and text.startswith(wm)


# ---------- ссылки ----------
def link_url(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith("@"):
        return f"https://t.me/{v[1:]}"
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith(("t.me/", "github.com/")):
        return "https://" + v
    return f"https://t.me/{v}"


def link_text(v: str) -> str:
    v = (v or "").strip()
    return v.replace("https://", "").replace("http://", "") if not v.startswith("@") else v


def links(cfg) -> list:
    """[(ключ, эмодзи, подпись, текст, url)] — только заполненные."""
    out = []
    for key, (emo, name) in LINK_NAMES.items():
        v = cfg.get(f"project.links.{key}", "")
        if v:
            out.append((key, emo, name, link_text(v), link_url(v)))
    return out


# ---------- описание бота (Telegram «Что умеет этот бот?» и «О себе») ----------
def bot_description(cfg) -> str:
    head = f"{SIGN} {TITLE_FANCY} v{VERSION} {SIGN}"
    tail = [f"{emo} {name}: {txt}" for _, emo, name, txt, _ in links(cfg)]
    feats = list(FEATURES)
    while True:
        text = "\n".join([head, ""] + feats + ([""] + tail if tail else []))
        if len(text) <= 512 or not feats:
            return text[:512]
        feats.pop()


def bot_short_description(cfg) -> str:
    upd = cfg.get("project.links.updates", "")
    base = f"{SIGN} {APP_NAME} — бот-помощник продавца"
    return (f"{base} · {link_text(upd)}" if upd else base)[:120]


def description_hash(cfg) -> str:
    return hashlib.sha1((bot_description(cfg) + "|" + bot_short_description(cfg)).encode()).hexdigest()[:12]


def links_html(cfg, esc) -> str:
    return "\n".join(f'{emo} {name}: <a href="{url}">{esc(txt)}</a>' for _, emo, name, txt, url in links(cfg))


BANNER_ART = r"""
   ____          _
  / ___|__ _ ___| |_ ___  _ __
 | |   / _` / __| __/ _ \| '__|
 | |__| (_| \__ \ || (_) | |
  \____\__,_|___/\__\___/|_|"""
