import asyncio
import json
import logging
import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import DB_PATH, PROFILE_PATH
from bot.handlers import (
    _retry_queue,
    check_business_hours,
    execute_call,
)
from core.agent import evaluate_context, get_call_summary
from telephony.call_manager import call_manager
from utils.db import buscar_historico, buscar_recados, buscar_resumos

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bot", tags=["dispatcher"])


class CallRequest(BaseModel):
    phone_number: str
    context: str


@router.get("/status")
async def api_status() -> dict:
    try:
        buscar_historico(1)
        db_ok = True
    except Exception:
        db_ok = False

    sinal = call_manager.get_signal()
    reg = call_manager.get_registration()
    horario = check_business_hours()

    return {
        "db": db_ok,
        "gsm_signal": sinal,
        "gsm_registration": reg,
        "gsm_online": sinal > 0,
        "call_active": call_manager.call_active,
        "business_hours": horario,
        "retry_queue_size": len(_retry_queue),
    }


@router.get("/perfil")
async def api_perfil() -> dict:
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Perfil não encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/historico")
async def api_historico() -> dict:
    rows = buscar_historico(5)
    return {
        "items": [
            {
                "data_hora": r[0],
                "numero": r[1],
                "contexto": r[2],
                "status": r[3],
                "resultado": r[4],
            }
            for r in rows
        ]
    }


@router.get("/recados")
async def api_recados() -> dict:
    rows = buscar_recados(5)
    return {
        "items": [
            {
                "data_hora": r[0],
                "numero_origem": r[1],
                "resumo": r[2],
                "status": r[3],
            }
            for r in rows
        ]
    }


@router.get("/fila")
async def api_fila() -> dict:
    return {"items": list(_retry_queue)}


@router.get("/resumo")
async def api_resumo() -> dict:
    return {"resumo": get_call_summary()}


@router.get("/transcricao")
async def api_transcricao() -> dict:
    rows = buscar_resumos(3)
    return {
        "items": [
            {
                "ligacao_id": r[0],
                "numero": r[1],
                "data_hora": r[2],
                "duracao_turnos": r[3],
                "resumo": r[4],
            }
            for r in rows
        ]
    }


@router.get("/evaluate")
async def api_evaluate(phone: str, context: str = "") -> dict:
    result = await evaluate_context(phone, context)
    return result


@router.post("/call/start")
async def api_call_start(req: CallRequest, background_tasks: BackgroundTasks) -> dict:
    if call_manager.call_active:
        raise HTTPException(status_code=409, detail="Ligação já em andamento")
    if not req.phone_number.strip():
        raise HTTPException(status_code=422, detail="phone_number não pode ser vazio")

    import os
    from core.agent import translate_to_japanese

    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    text_jp = await translate_to_japanese(req.context)

    background_tasks.add_task(execute_call, req.phone_number, req.context, text_jp, chat_id)
    return {
        "status": "dispatched",
        "phone_number": req.phone_number,
        "context": req.context,
        "text_jp": text_jp,
    }


@router.post("/desligar")
async def api_desligar() -> dict:
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None, lambda: call_manager._send_at("AT+CHUP", wait=3.0)
    )
    call_manager.call_active = False
    return {"raw_response": raw, "call_active": call_manager.call_active}


@router.post("/limpar")
async def api_limpar() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM ligacoes_saida")
        c.execute("DELETE FROM ligacoes_recebidas")
        conn.commit()
        conn.close()
        _retry_queue.clear()
        return {"status": "ok", "message": "Histórico e fila limpos"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skip")
async def api_skip(req: CallRequest, background_tasks: BackgroundTasks) -> dict:
    import os
    from core.agent import translate_to_japanese

    if call_manager.call_active:
        raise HTTPException(status_code=409, detail="Ligação já em andamento")
    if not req.phone_number.strip():
        raise HTTPException(status_code=422, detail="phone_number não pode ser vazio")

    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    text_jp = await translate_to_japanese(req.context)
    background_tasks.add_task(execute_call, req.phone_number, req.context, text_jp, chat_id)
    return {
        "status": "dispatched",
        "phone_number": req.phone_number,
        "context": req.context,
        "text_jp": text_jp,
    }


@router.post("/retentar")
async def api_retentar(background_tasks: BackgroundTasks) -> dict:
    import os
    from core.agent import translate_to_japanese

    if not _retry_queue:
        raise HTTPException(status_code=404, detail="Fila vazia")

    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    item = _retry_queue.pop(0)
    text_jp = await translate_to_japanese(item["contexto"])
    background_tasks.add_task(execute_call, item["numero"], item["contexto"], text_jp, chat_id)
    return {
        "status": "dispatched",
        "numero": item["numero"],
        "contexto": item["contexto"],
    }
