import logging
import os
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from bot import router, init_db  # импортируем роутер и init_db из bot.py

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-secret")
WEBHOOK_PATH = "/webhook"

BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL")
PORT = int(os.getenv("PORT", "10000"))

dp = Dispatcher()
dp.include_router(router)


async def on_startup(bot: Bot) -> None:
    await init_db()
    if not BASE_URL:
        raise RuntimeError("Нет RENDER_EXTERNAL_URL/WEBHOOK_BASE_URL")

    await bot.set_webhook(
        url=f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )


async def on_shutdown(bot: Bot) -> None:
    # можно не удалять webhook, но так аккуратнее
    await bot.delete_webhook(drop_pending_updates=False)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
