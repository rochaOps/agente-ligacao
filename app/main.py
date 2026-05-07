import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram.ext import Application

from api.bot_routes import router as bot_router, set_telegram_app as set_api_telegram_app
from bot.handlers import set_telegram_app, handle_incoming_ring, register_handlers
from config import validate_env
from core.stt import load_model
from telephony.call_manager import call_manager
from utils.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_env()

    logger.info("Inicializando banco de dados...")
    init_db()

    logger.info("Pré-carregando modelo Whisper...")
    load_model()

    logger.info("Inicializando SIM7600G-H...")
    if call_manager.initialize():
        logger.info("SIM7600G-H inicializado com sucesso!")
        call_manager.on_ring = handle_incoming_ring
    else:
        logger.warning("SIM7600G-H não disponível — modo sem GSM")

    logger.info("Iniciando bot Telegram...")
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    set_telegram_app(telegram_app)
    set_api_telegram_app(telegram_app)

    register_handlers(telegram_app)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot Telegram pronto — polling ativo.")

    yield

    logger.info("Encerrando...")
    call_manager.disconnect()
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.include_router(bot_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
