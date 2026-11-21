# Плагины FunPay & Playerok Castor

Плагин — это один `.py` файл в папке `plugins/` (или папка с `__init__.py`). Установка: мини-апп → «Ещё» → «Плагины», отправить файл боту в Telegram или положить в папку и перезапустить бота.

## Минимальный плагин (сразу для FunPay и Playerok)

```python
NAME = "Мой плагин"
VERSION = "1.0"
DESCRIPTION = "Что делает плагин"
CREDITS = "@me"
UUID = "c0ffee00-0000-4000-8000-000000000001"   # уникальный, сгенерируйте свой
SETTINGS_PAGE = False


def on_message(cardinal, msg):
    # msg.platform: "funpay" | "playerok", msg.text, msg.author, msg.chat_id, msg.by_me
    if not msg.by_me and msg.text.lower() == "!пинг":
        msg.reply("понг")


def on_order(cardinal, order):
    # order.platform, order.id, order.title, order.price, order.buyer, order.amount
    cardinal.castor.notify("delivery", f"Новый заказ {order.title}")


BIND_TO_ANY_MESSAGE = [on_message]
BIND_TO_ANY_ORDER = [on_order]
```

## Хуки

| Хук | Аргументы | Когда |
|---|---|---|
| `BIND_TO_PRE_INIT`, `BIND_TO_POST_INIT` | `(cardinal)` | при запуске (до и после Telegram) |
| `BIND_TO_POST_START` | `(cardinal)` | после входа в FunPay |
| `BIND_TO_PRE_STOP`, `BIND_TO_POST_STOP` | `(cardinal)` | при остановке |
| `BIND_TO_ANY_MESSAGE` | `(cardinal, UMessage)` | сообщение на любой площадке |
| `BIND_TO_ANY_ORDER` | `(cardinal, UOrder)` | заказ на любой площадке |
| `BIND_TO_NEW_MESSAGE` | `(cardinal, NewMessageEvent)` | FunPay, формат Cardinal |
| `BIND_TO_NEW_ORDER` | `(cardinal, NewOrderEvent)` | FunPay, формат Cardinal |
| `BIND_TO_ORDER_STATUS_CHANGED` | `(cardinal, event)` | FunPay |
| `BIND_TO_PRE_DELIVERY`, `BIND_TO_POST_DELIVERY` | `(cardinal, UOrder, rule)` | автовыдача |
| `BIND_TO_PRE_LOTS_RAISE`, `BIND_TO_POST_LOTS_RAISE` | `(cardinal, groups)` | поднятие лотов |
| `BIND_TO_PL_NEW_MESSAGE`, `BIND_TO_PL_NEW_DEAL`, `BIND_TO_PL_DEAL_CONFIRMED`, `BIND_TO_PL_NEW_REVIEW`, `BIND_TO_PL_<ТИП>` | `(cardinal, event)` | события Playerok (из playerokapi) |
| `BIND_TO_DELETE` | `(cardinal)` | плагин удаляют |

## Что доступно в `cardinal`
- `cardinal.account` — клиент FunPay: `send_message(chat_id, text)`, `get_order(id)`, `refund(id)`, `get_chats()`, `get_chat_history(chat_id)`, `send_review(order_id, text)`, `raise_lots(game_id, nodes)`, `id`, `username`, `balance`.
- `cardinal.playerok` — мост Playerok: `acc` (объект playerokapi), `send_message(chat_id, text)`, `mark_sent(deal_id)`.
- `cardinal.castor` — ядро Castor: `send(platform, chat_id, text)`, `notify(kind, html)`, `cfg`, `stats`, `customers`, `deliver(order)`.
- `cardinal.telegram` — Telegram: `bot` (TeleBot), `msg_handler(fn, commands=[...])`, `cbq_handler(fn, func=...)`, `set_state / get_state / clear_state`.
- `cardinal.add_telegram_commands(UUID, [("cmd", "описание", True)])`.

## Плагины FunPay Cardinal
Обычные плагины Cardinal ставятся как есть. Импорты `FunPayAPI`, `cardinal`, `tg_bot`, `Utils`, `locales` подменяются совместимыми заглушками. Простые плагины (автоответы, уведомления, работа с заказами и чатами) работают. Плагины, которые глубоко используют внутренности Cardinal, могут работать частично — смотрите логи.

Кнопка «Настройки» плагина в Telegram отправляет callback `PlSet:<UUID>:0`, как в Cardinal.

## Модули Playerok Universal
Папку модуля можно положить в `plugins/`. Подхватываются `PLAYEROK_EVENT_HANDLERS` (обработчики получают `(plbot, event)`, у `plbot` есть `.account` и `.send_message`) и `BOT_EVENT_HANDLERS` (`ON_INIT`, `ON_PLAYEROK_BOT_INIT`). `TELEGRAM_BOT_ROUTERS` (aiogram) не поддерживаются. Зависимости из `requirements.txt` модуля ставятся автоматически.
