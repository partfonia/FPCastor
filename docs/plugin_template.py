"""Шаблон плагина Castor с собственным экраном настроек — как у встроенных модулей.

Скопируйте файл в папку plugins/ под своим именем, поменяйте UUID, KEY и логику — и перезапустите бота.
Модуль появится в Telegram (/menu → «🧩 Модули») и в панели (Настройки → Модули): включение,
поля настроек и кнопки действий рисуются автоматически по FIELDS и actions().
"""
from castor.core import fmt_template
from castor.modules import Module

NAME = "Мой модуль"
VERSION = "1.0"
DESCRIPTION = "Пример: отвечает на !бонус и считает, сколько раз его спросили"
CREDITS = "@me"
UUID = "c0ffee00-0000-4000-8000-0000000000aa"   # сгенерируйте свой, например на uuidgenerator.net


class BonusModule(Module):
    KEY = "bonus"                 # латиница, уникальный — по нему хранятся настройки: modules.bonus
    NAME = "Бонус по команде"
    ICON = "🎁"
    DESCRIPTION = "Покупатель пишет !бонус — бот отвечает промокодом."
    PLATFORMS = ("funpay", "playerok")

    # настройки по умолчанию (добавляются в config.json при первом запуске)
    DEFAULTS = {"command": "!бонус", "text": "Ваш промокод: $code", "code": "CASTOR10", "only_buyers": True}

    # экран настроек: (путь, подпись, тип, подсказка). Типы: bool, text, area, number, lines, select, items
    FIELDS = [
        ("command", "Команда", "text"),
        ("code", "Промокод", "text"),
        ("text", "Ответ", "area", "$code — промокод, $username — ник"),
        ("only_buyers", "Только тем, кто уже покупал", "bool"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.data.setdefault("asked", 0)   # данные модуля хранятся в castor_data/modules/bonus.json

    def on_message(self, m) -> bool:
        if (m.text or "").strip().lower() != self.get("command", "").lower():
            return False                    # не наше — пусть обработают другие
        if self.get("only_buyers") and not self.app.customers.get(m.platform, m.author):
            self.reply(m.platform, m.chat_id, "Бонус доступен после первой покупки 🙂", m.account, m.chat_name)
            return True
        self.data["asked"] += 1
        self.save()
        self.reply(m.platform, m.chat_id, fmt_template(self.get("text"), code=self.get("code"), username=m.author),
                   m.account, m.chat_name)
        return True                         # обработано — автоответчик это сообщение не трогает

    def on_order(self, o) -> bool:
        return False                        # True — если модуль сам выдал заказ (обычная автовыдача пропустится)

    def tick(self):
        pass                                # вызывается примерно раз в 30 секунд

    def status(self) -> str:
        return f"спросили {self.data['asked']} раз"

    def actions(self) -> list:
        return [("reset", "🔄 Обнулить счётчик")]

    def run_action(self, action, args) -> str:
        if action == "reset":
            self.data["asked"] = 0
            self.save()
            return "Счётчик обнулён"
        return "Нет такого действия"


CASTOR_MODULE = BonusModule
