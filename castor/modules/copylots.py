"""Копирование лотов между вашими аккаунтами FunPay: название, описание, цена, поля категории.

Укажите, откуда и куда копировать (и при желании — только некоторые разделы), и нажмите «Скопировать».
Картинки и товары автовыдачи FunPay не переносятся — их нужно добавить на новом аккаунте.
"""
from __future__ import annotations

import threading
import time

from ..core import esc, log
from . import Module


class CopyLotsModule(Module):
    KEY = "copylots"
    GROUP = "Лоты и цены"
    ORDER = 50
    NAME = "CopyLots"
    ICON = "📑"
    DESCRIPTION = "Переносит лоты с одного аккаунта FunPay на другой: текст, цена и поля категории."
    PLATFORMS = ("funpay",)
    DEFAULTS = {"source": "", "target": "", "nodes": [], "only_active": True}
    FIELDS = [
        ("source", "Откуда (ID аккаунта, например fp1)", "text"),
        ("target", "Куда (ID аккаунта, например fp2)", "text"),
        ("nodes", "Только эти разделы (ID, по одному в строке)", "lines", "пусто — все разделы"),
        ("only_active", "Только активные лоты", "bool"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self.data.setdefault("last", "")

    def status(self) -> str:
        return "копирую…" if self._busy else (self.data.get("last") or "готов")

    def actions(self) -> list:
        return [("run", "▶️ Скопировать лоты")]

    def run_action(self, action, args) -> str:
        if action != "run":
            return "Нет такого действия"
        if self._busy:
            return "Уже копирую — дождитесь окончания"
        src = self.app.service("funpay", self.get("source") or None)
        dst = self.app.service("funpay", self.get("target") or None)
        if not src or not dst or not src.ok or not dst.ok:
            return "Оба аккаунта должны быть подключены и работать (укажите их ID в настройках модуля)"
        if src is dst:
            return "Аккаунты «откуда» и «куда» совпадают"
        self._busy = True
        threading.Thread(target=self._copy, args=(src, dst), daemon=True).start()
        return "Начал копировать. Пришлю отчёт, когда закончу."

    def _copy(self, src, dst):
        ok, fail = 0, []
        try:
            only = {str(n).strip() for n in self.get("nodes", []) if str(n).strip()}
            lots = src.client.get_my_lots()
            lots = [x for x in lots if (not only or str(x["node"]) in only) and (x["active"] or not self.get("only_active", True))]
            for lot in lots:
                try:
                    fields = src.client.get_lot_form(lot["id"])
                    dst.client.create_lot_from(fields, lot["node"])
                    ok += 1
                except Exception as e:
                    fail.append(f"{lot.get('title', lot['id'])[:40]}: {e}")
                time.sleep(2)
            self.data["last"] = f"последний раз: скопировано {ok}, ошибок {len(fail)}"
            self.save()
            log.info("📑 Копирование лотов: %s успешно, %s с ошибками", ok, len(fail))
            self.notify(f"📑 <b>Копирование лотов</b>: скопировано {ok} из {len(lots)}"
                        + ("\n\nНе получилось:\n" + "\n".join(f"• {esc(x)}" for x in fail[:10]) if fail else ""), kind="start")
        finally:
            self._busy = False
