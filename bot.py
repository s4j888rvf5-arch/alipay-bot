import json
import os
import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import LinkPreviewOptions, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ---------- настройки из переменных окружения ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]                    # токен от @BotFather
ADMIN_ID = int(os.environ["ADMIN_ID"])                 # ваш Telegram ID (@userinfobot)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme123")  # любые буквы/цифры
RATE_SEED = os.environ.get("RATE_SEED", "alipay")      # любое слово: от него зависят «случайные» курсы
HEADER = os.environ.get("HEADER", "Курс обмена")       # заголовок сообщения
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = "/webhook"

TZ = ZoneInfo("Europe/Moscow")
PERIOD_DAYS = 2        # курс меняется раз в 2 дня
SELL_FACTOR = 0.952    # USDT продажа = USDT покупка * 0.952

# Настройки, которые хранятся в закреплённом сообщении в вашем чате с ботом
CFG = {
    "mode": "auto",                                   # auto | manual
    "min": 14.0,                                      # границы авто-курса, руб за 1 CNY
    "max": 15.0,
    "manual_cny": 14.5,                               # курс в ручном режиме
    "since": "",                                      # дата ручного курса
    "usdt_cny": 6.54,                                 # сколько юаней за 1 USDT
    "manager": os.environ.get("MANAGER_USERNAME", "your_manager"),
}
STATE = {"msg_id": None}


# ---------- хранение настроек ----------
async def load_cfg(bot: Bot) -> None:
    try:
        chat = await bot.get_chat(ADMIN_ID)
        pm = chat.pinned_message
        if pm and pm.text and pm.text.startswith("CFG "):
            CFG.update(json.loads(pm.text[4:]))
            STATE["msg_id"] = pm.message_id
    except Exception as e:  # админ ещё не писал боту и т.п.
        print("load_cfg:", e)


async def save_cfg(bot: Bot) -> None:
    msg = await bot.send_message(
        ADMIN_ID, "CFG " + json.dumps(CFG, ensure_ascii=False),
        parse_mode=None, disable_notification=True,
    )
    await bot.pin_chat_message(ADMIN_ID, msg.message_id, disable_notification=True)
    old = STATE["msg_id"]
    if old:
        try:
            await bot.delete_message(ADMIN_ID, old)
        except Exception:
            pass
    STATE["msg_id"] = msg.message_id


# ---------- расчёт курса ----------
def current_rates() -> dict:
    today = datetime.now(TZ).date()
    if CFG["mode"] == "manual":
        cny = CFG["manual_cny"]
        since = CFG["since"] or today.strftime("%d.%m.%Y")
    else:
        period = today.toordinal() // PERIOD_DAYS
        rnd = random.Random(f"{RATE_SEED}:{period}")
        span = CFG["max"] - CFG["min"]
        # отступ 5% от границ, чтобы не выпадали «круглые» 14,00 и 15,00
        cny = round(rnd.uniform(CFG["min"] + span * 0.05, CFG["max"] - span * 0.05), 5)
        since = date.fromordinal(period * PERIOD_DAYS).strftime("%d.%m.%Y")
    usdt_buy = cny * CFG["usdt_cny"]
    return {
        "cny_rub": cny,
        "usdt_cny": CFG["usdt_cny"],
        "usdt_buy": usdt_buy,
        "usdt_sell": usdt_buy * SELL_FACTOR,
        "since": since,
    }


def fmt(x: float, digits: int = 0) -> str:
    return f"{x:,.{digits}f}".replace(",", "\u00a0").replace(".", ",")


def manager_url() -> str:
    return f"https://t.me/{CFG['manager'].lstrip('@')}"


def rate_text() -> str:
    r = current_rates()
    return (
        f"🌏 <b>{HEADER}</b>\n"
        f"└ Курс действует с {r['since']}\n\n"
        "🏮 <b>CNY</b>\n"
        f"├ <b>CNY - RUB:</b> {fmt(r['cny_rub'], 2)}\n"
        f"└ {fmt(r['cny_rub'] * 1000, 2)} RUB = 1000 CNY\n\n"
        "💵 <b>USDT</b>\n"
        f"├ <b>USDT - CNY:</b> {fmt(r['usdt_cny'], 2)}\n"
        f"└ {fmt(1000 / r['usdt_cny'])} USDT = 1000 CNY\n\n"
        "<blockquote>"
        "↙️ <b>USDT ПОКУПКА</b>\n"
        f"├ <b>USDT:</b> {fmt(r['usdt_buy'], 2)}\n"
        f"└ {fmt(r['usdt_buy'] * 1000, 2)} RUB = 1000 USDT\n\n"
        "↗️ <b>USDT ПРОДАЖА</b>\n"
        f"├ <b>USDT ASK:</b> {fmt(r['usdt_sell'], 2)}\n"
        f"└ {fmt(r['usdt_sell'] * 1000, 2)} RUB = 1000 USDT"
        "</blockquote>\n\n"
        f'🤝 <b><a href="{manager_url()}">ТВОЙ НАДЁЖНЫЙ ПАРТНЁР</a></b>'
    )


async def reply_with_rate(message: Message):
    await message.answer(
        rate_text(),
        link_preview_options=LinkPreviewOptions(
            is_disabled=False,
            url=manager_url(),       # карточка аккаунта менеджера под текстом
            prefer_small_media=True,
            show_above_text=False,
        ),
    )


dp = Dispatcher()


@dp.message(CommandStart())
@dp.message(Command("rate"))
async def send_rate(message: Message):
    await reply_with_rate(message)


# ---------- команды администратора ----------
def to_float(s: str) -> float:
    return float(s.replace(",", "."))


@dp.message(Command("setrate"), F.from_user.id == ADMIN_ID)
async def set_rate(message: Message, bot: Bot):
    """/setrate 14.5  или  /setrate 14.5 6.54  (ручной курс)"""
    parts = (message.text or "").split()[1:]
    try:
        values = [to_float(p) for p in parts]
        assert 1 <= len(values) <= 2 and all(v > 0 for v in values)
    except Exception:
        await message.answer("Формат: <code>/setrate 14.5</code> или <code>/setrate 14.5 6.54</code>\n"
                             "(курс CNY→RUB и, по желанию, USDT→CNY)")
        return
    CFG["mode"] = "manual"
    CFG["manual_cny"] = values[0]
    CFG["since"] = datetime.now(TZ).strftime("%d.%m.%Y")
    if len(values) == 2:
        CFG["usdt_cny"] = values[1]
    await save_cfg(bot)
    await message.answer("✅ Ручной курс включён. Вернуть авто: /auto")


@dp.message(Command("auto"), F.from_user.id == ADMIN_ID)
async def set_auto(message: Message, bot: Bot):
    CFG["mode"] = "auto"
    await save_cfg(bot)
    await message.answer(f"✅ Авто-курс {CFG['min']}–{CFG['max']} ₽, смена раз в {PERIOD_DAYS} дня")


@dp.message(Command("range"), F.from_user.id == ADMIN_ID)
async def set_range(message: Message, bot: Bot):
    """/range 14 15"""
    parts = (message.text or "").split()[1:]
    try:
        lo, hi = (to_float(p) for p in parts)
        assert 0 < lo < hi
    except Exception:
        await message.answer("Формат: <code>/range 14 15</code>")
        return
    CFG["min"], CFG["max"] = lo, hi
    await save_cfg(bot)
    await message.answer(f"✅ Диапазон: {lo}–{hi} ₽ за 1 CNY")


@dp.message(Command("setmanager"), F.from_user.id == ADMIN_ID)
async def set_manager(message: Message, bot: Bot):
    """/setmanager @username"""
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: <code>/setmanager @username</code>")
        return
    CFG["manager"] = parts[1].lstrip("@")
    await save_cfg(bot)
    await message.answer(f"✅ Менеджер: @{CFG['manager']}")


@dp.message(Command("status"), F.from_user.id == ADMIN_ID)
async def status(message: Message):
    r = current_rates()
    await message.answer(
        f"Режим: <b>{CFG['mode']}</b>\n"
        f"Диапазон: {CFG['min']}–{CFG['max']} ₽\n"
        f"Сейчас: {fmt(r['cny_rub'], 4)} ₽ за 1 CNY (с {r['since']})\n"
        f"Менеджер: @{CFG['manager']}"
    )


# любое другое сообщение тоже получает курс (регистрировать строго последним)
@dp.message(F.text)
async def any_text(message: Message):
    await reply_with_rate(message)


# ---------- запуск (webhook) ----------
async def on_startup(bot: Bot):
    await load_cfg(bot)
    await bot.set_webhook(
        f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )


def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


if __name__ == "__main__":
    main()
