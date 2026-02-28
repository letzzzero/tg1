import os
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# === Подстрой под свой bot.py ===
# Предполагается, что в bot.py у тебя есть router и init_db()
# Если имена другие — просто поправь import
from bot import router, init_db

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-secret")
WEBHOOK_PATH = "/webhook"

# Render сам дает эти переменные веб-сервису
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL")
PORT = int(os.getenv("PORT", "10000"))

dp = Dispatcher()
dp.include_router(router)


async def on_startup(bot: Bot) -> None:
    await init_db()

    if not BASE_URL:
        raise RuntimeError(
            "Не найден BASE_URL. На Render обычно есть RENDER_EXTERNAL_URL. "
            "Либо задай WEBHOOK_BASE_URL вручную."
        )

    # Регистрируем webhook при старте
    await bot.set_webhook(
        url=f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )


async def on_shutdown(bot: Bot) -> None:
    # Удалять webhook не обязательно, но нормально для аккуратного shutdown
    await bot.delete_webhook(drop_pending_updates=False)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    bot = Bot(token=BOT_TOKEN)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    # Хэндлер для Telegram webhook
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    # Подключаем startup/shutdown aiogram к aiohttp
    setup_application(app, dp, bot=bot)

    # Render требует слушать 0.0.0.0 и порт (обычно из PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
