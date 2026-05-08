import asyncio
import logging
import os
import time
from datetime import datetime

import pytz
from telegram.ext import Application

from config import (
    MAX_INCOMING_TURNS, MAX_RECORD_DURATION,
    CALL_ANSWER_TIMEOUT, SILENCE_STREAK_MAX, CONFIDENCE_MIN, CONFIDENCE_UNCERTAIN,
    TIMEZONE,
)
from core.agent import (
    translate_to_portuguese,
    start_call_session, get_call_summary, get_full_transcript,
    process_call_turn, generate_call_summary, process_incoming_call, get_user_name,
)
from core.stt import speech_to_text
from core.tts import text_to_speech
from telephony.audio import audio_manager
from telephony.call_manager import call_manager
from utils.db import (
    salvar_ligacao_saida, salvar_turno_transcricao, salvar_resumo,
)

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
JST = pytz.timezone(TIMEZONE)

_retry_queue: list[dict]           = []
_telegram_app: Application | None  = None
_main_loop: asyncio.AbstractEventLoop | None = None
_pending_context: dict[int, dict]  = {}  # chat_id → {phone, context_text, expires_at}


def set_telegram_app(app: Application) -> None:
    global _telegram_app, _main_loop
    _telegram_app = app
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def check_business_hours() -> dict:
    now  = datetime.now(JST)
    hora = now.hour
    dia  = now.weekday()
    dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    is_weekend       = dia >= 5
    is_business_hours = 9 <= hora < 17
    is_open = not is_weekend and is_business_hours
    aviso   = None
    if not is_business_hours:
        aviso = f"⚠️ Aviso: {now.strftime('%H:%M')} JST — fora do horário comercial padrão (9h-17h)"
    elif is_weekend:
        aviso = f"⚠️ Aviso: {dias[dia]} — fim de semana. Alguns serviços podem não atender"
    return {
        "is_open":     is_open,
        "hora_atual":  now.strftime("%H:%M JST"),
        "dia_semana":  dias[dia],
        "aviso":       aviso,
    }


# ── Main call loop ────────────────────────────────────────────────────────────

async def execute_call(phone: str, context_text: str, text_jp: str, chat_id: int) -> None:
    bot        = _telegram_app.bot
    loop       = asyncio.get_running_loop()
    wav_path: str | None = None
    ligacao_id: int | None = None

    try:
        user_name = get_user_name()
        intro = (
            f"お忙しいところ失礼いたします。"
            f"{user_name}の代理でご連絡しております。"
            f"{text_jp}"
        )
        wav_path = await text_to_speech(intro, beep=True)

        await bot.send_message(chat_id, f"📞 Discando para {phone}...")
        if not await loop.run_in_executor(None, lambda: call_manager.dial(phone)):
            await bot.send_message(chat_id, "❌ Erro ao discar. Verifique o módulo GSM.")
            return

        await bot.send_message(chat_id, "⏳ Aguardando atender...")
        for i in range(CALL_ANSWER_TIMEOUT):
            await asyncio.sleep(1)
            if call_manager.call_active:
                break
            if call_manager.call_failed:
                await bot.send_message(chat_id, "📵 Linha ocupada ou fora de serviço")
                await loop.run_in_executor(None, call_manager.hangup)
                return
            if i == 14:
                await bot.send_message(
                    chat_id,
                    f"⏳ Ainda aguardando... ({i+1}s)\nSinal: {call_manager.get_signal()}/31"
                )
        else:
            await bot.send_message(
                chat_id,
                "❌ Ninguém atendeu (30s sem VOICE CALL: BEGIN).\n"
                "💡 Verifique se o evento chega no ttyUSB3 com /debug/at/AT+CLCC"
            )
            await loop.run_in_executor(None, call_manager.hangup)
            return

        await bot.send_message(chat_id, "✅ Ligação conectada!")
        await loop.run_in_executor(None, call_manager.enable_pcm_audio)
        start_call_session()
        ligacao_id = salvar_ligacao_saida(phone, context_text, text_jp, status="em_andamento")

        turn                  = 0
        silence_streak        = 0
        in_hold               = False
        hold_silence_count    = 0
        ended_by_audio_failure = False
        call_start_time       = time.monotonic()
        call_manager.call_end_reason = None

        while call_manager.call_active:
            turn += 1
            if wav_path:
                await audio_manager.play_and_wait(wav_path)
                os.unlink(wav_path)
                wav_path = None

            if not call_manager.call_active:
                break

            await asyncio.sleep(0.4)
            await bot.send_message(chat_id, f"👂 Turno {turn} — ouvindo atendente...")
            t_turn_start = time.monotonic()
            recorded, heard_speech = await audio_manager.record_turn(duration=MAX_RECORD_DURATION)
            t_rec_done   = time.monotonic()
            result       = await loop.run_in_executor(None, lambda: speech_to_text(recorded))
            t_stt_done   = time.monotonic()
            rec_s  = t_rec_done - t_turn_start
            stt_s  = t_stt_done - t_rec_done

            async def _send_audio_and_cleanup(path: str, t: int) -> None:
                try:
                    with open(path, "rb") as f:
                        await bot.send_voice(
                            chat_id=chat_id, voice=f,
                            caption=f"🎙 Turno {t} — áudio bruto ({rec_s:.1f}s)"
                        )
                finally:
                    os.unlink(path)

            asyncio.create_task(_send_audio_and_cleanup(recorded, turn))

            confianca = result["confianca"]
            texto     = result["texto"].strip()

            if not texto or confianca < CONFIDENCE_MIN or not heard_speech:
                asyncio.create_task(bot.send_message(
                    chat_id,
                    f"⏱ Gravação: {rec_s:.1f}s | STT: {stt_s:.1f}s | confiança: {confianca:.0%}"
                ))
                if in_hold:
                    hold_silence_count += 1
                    logger.warning(
                        f"HOLD: turno {turn} — silêncio durante espera "
                        f"({hold_silence_count}/10), silence_streak NÃO incrementado"
                    )
                    if hold_silence_count >= 10:
                        call_manager.call_end_reason = "agent_hold_timeout"
                        logger.warning("HOLD: espera máxima (10 turnos) — encerrando ligação")
                        closing = await text_to_speech(
                            "大変恐れ入ります。またあらためてご連絡いたします。", beep=False
                        )
                        await audio_manager.play_and_wait(closing)
                        os.unlink(closing)
                        ended_by_audio_failure = True
                        asyncio.create_task(bot.send_message(
                            chat_id, "⏸ Hold excessivo (10 turnos) — encerrando ligação."
                        ))
                        break
                    asyncio.create_task(bot.send_message(
                        chat_id, f"⏸ Hold: aguardando retorno ({hold_silence_count}/10)..."
                    ))
                    continue
                silence_streak += 1
                logger.warning(f"SILENCE: turno {turn}, streak {silence_streak}")
                if silence_streak >= SILENCE_STREAK_MAX:
                    asyncio.create_task(bot.send_message(
                        chat_id,
                        f"⚠️ Áudio difícil ({silence_streak} turnos) — mantendo ligação, pedindo para repetir..."
                    ))
                    silence_streak = 0
                else:
                    asyncio.create_task(bot.send_message(
                        chat_id, f"🔇 Silêncio ({silence_streak}/{SILENCE_STREAK_MAX}) — pedindo para repetir..."
                    ))
                wav_path = await text_to_speech(
                    "恐れ入りますが、もう一度おっしゃっていただけますでしょうか。", beep=True
                )
                continue

            silence_streak = 0
            if in_hold:
                in_hold = False
                hold_silence_count = 0
                logger.info(f"HOLD: atendente retornou no turno {turn} — espera encerrada")
            uncertain = confianca < CONFIDENCE_UNCERTAIN
            status    = "❓ incerto" if uncertain else "✅"
            asyncio.create_task(bot.send_message(
                chat_id, f"👂 Atendente: {texto} ({confianca:.0%} {status})"
            ))

            async def _send_pt(jp: str) -> None:
                try:
                    pt = await translate_to_portuguese(jp)
                    await bot.send_message(chat_id, f"🇧🇷 Atendente (PT): {pt}")
                except Exception:
                    pass

            asyncio.create_task(_send_pt(texto))

            if ligacao_id:
                salvar_turno_transcricao(ligacao_id, phone, turn, "atendente", texto)

            elapsed = time.monotonic() - call_start_time
            t_llm = time.monotonic()
            try:
                response = await process_call_turn(
                    texto,
                    uncertain=uncertain,
                    in_hold=in_hold,
                    turn=turn,
                    elapsed_seconds=elapsed,
                )
            except Exception as e:
                logger.error(f"LLM timeout/erro: {e}")
                asyncio.create_task(bot.send_message(chat_id, f"⚠️ LLM erro: {e}"))
                wav_path = await text_to_speech("少々お待ちください。", beep=True)
                continue
            t_llm_done = time.monotonic()

            is_end   = response.get("is_end", False)
            wav_path = await text_to_speech(response["resposta_jp"], beep=not is_end)
            t_tts    = time.monotonic()
            total_s  = t_tts - t_rec_done

            asyncio.create_task(bot.send_message(
                chat_id,
                f"⏱ Gravação: {rec_s:.1f}s | STT: {stt_s:.1f}s | "
                f"LLM: {t_llm_done - t_llm:.1f}s | TTS: {t_tts - t_llm_done:.1f}s | total: {total_s:.1f}s"
            ))
            asyncio.create_task(bot.send_message(chat_id, f"🤖 Agente: {response['resposta_jp']}"))

            if ligacao_id:
                salvar_turno_transcricao(ligacao_id, phone, turn, "agente", response["resposta_jp"])

            if response["is_transfer"]:
                asyncio.create_task(bot.send_message(chat_id, "🔀 Transferindo ligação..."))
            if response["is_hold"]:
                in_hold = True
                hold_silence_count = 0
                logger.warning(
                    f"HOLD detectado no turno {turn} — silence_streak pausado, "
                    f"aguardando retorno da atendente"
                )
                asyncio.create_task(bot.send_message(chat_id, "⏸ Em espera..."))
            if is_end:
                call_manager.call_end_reason = "agent_end_detected"
                logger.warning(f"END detectado no turno {turn} — despedida pela LLM")
                asyncio.create_task(bot.send_message(
                    chat_id, "👋 Despedida detectada — encerrando após resposta."
                ))
                await audio_manager.play_and_wait(wav_path)
                os.unlink(wav_path)
                wav_path = None
                break

        if wav_path and call_manager.call_active:
            await audio_manager.play_and_wait(wav_path)
            os.unlink(wav_path)
            wav_path = None

        audio_manager.stop_playback()
        audio_manager.stop_recording()
        await loop.run_in_executor(None, call_manager.disable_pcm_audio)
        await asyncio.sleep(0.3)
        if not call_manager.call_end_reason:
            if not call_manager.call_active:
                call_manager.call_end_reason = "remote_hangup"
        logger.warning(
            f"=== CHAMADA ENCERRADA === razão: {call_manager.call_end_reason} "
            f"| turno: {turn} | em_hold: {in_hold}"
        )
        await loop.run_in_executor(None, call_manager.hangup)

        resumo = await generate_call_summary(context_text)
        await bot.send_message(
            chat_id, f"📝 *Resumo da ligação:*\n\n{resumo}", parse_mode="Markdown"
        )
        if ligacao_id:
            salvar_resumo(ligacao_id, phone, resumo, get_full_transcript(), turn)
        salvar_ligacao_saida(phone, context_text, text_jp, status="concluida", resultado=resumo)

        if ended_by_audio_failure:
            await bot.send_message(
                chat_id,
                "⚠️ *Ligação encerrada — hold excessivo*\n"
                "Atendente não retornou após 10 turnos de espera.\n"
                "Considere religar em outro momento.",
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Erro na ligação: {e}", exc_info=True)
        await bot.send_message(chat_id, f"❌ Erro durante ligação: {e}")
        if wav_path and os.path.exists(wav_path):
            os.unlink(wav_path)
        await loop.run_in_executor(None, call_manager.hangup)
    finally:
        start_call_session()


# ── Incoming call ─────────────────────────────────────────────────────────────

def handle_incoming_ring(number: str) -> None:
    """Called from the background serial listener thread — must not block."""
    if _telegram_app is None or _main_loop is None:
        return
    asyncio.run_coroutine_threadsafe(_handle_incoming_call_async(number), _main_loop)


async def _handle_incoming_call_async(number: str) -> None:
    bot  = _telegram_app.bot
    loop = asyncio.get_running_loop()
    try:
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"📲 Ligação recebida de: `{number or 'desconhecido'}`",
            parse_mode="Markdown"
        )
        await loop.run_in_executor(None, call_manager.answer)
        await loop.run_in_executor(None, call_manager.enable_pcm_audio)
        await asyncio.sleep(0.5)
        start_call_session()

        turn = 0
        while call_manager.call_active and turn < MAX_INCOMING_TURNS:
            turn += 1
            recorded, _ = await audio_manager.record_turn(duration=MAX_RECORD_DURATION)
            result   = await loop.run_in_executor(None, lambda: speech_to_text(recorded))
            os.unlink(recorded)
            if not result["texto"].strip() or result["confianca"] < CONFIDENCE_UNCERTAIN:
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"🔇 [{turn}] Silêncio ou ruído (confiança: {result['confianca']:.0%})"
                )
                break
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"👂 [{turn}] Chamador: {result['texto']} ({result['confianca']:.0%})"
            )
            response_jp = await process_incoming_call(result["texto"])
            await bot.send_message(TELEGRAM_CHAT_ID, f"🤖 [{turn}] Agente: {response_jp}")
            wav_path = await text_to_speech(response_jp)
            await audio_manager.play_and_wait(wav_path)
            os.unlink(wav_path)

        audio_manager.stop_playback()
        audio_manager.stop_recording()
        await loop.run_in_executor(None, call_manager.disable_pcm_audio)
        await asyncio.sleep(0.3)
        await loop.run_in_executor(None, call_manager.hangup)
        from utils.db import salvar_ligacao_recebida
        salvar_ligacao_recebida(number, get_call_summary(), "Ligação recebida e atendida pelo agente.")
        await bot.send_message(TELEGRAM_CHAT_ID, "📵 Ligação recebida encerrada.")
    except Exception as e:
        logger.error(f"Erro ao atender ligação recebida: {e}", exc_info=True)
        await loop.run_in_executor(None, call_manager.disable_pcm_audio)
        await loop.run_in_executor(None, call_manager.hangup)
    finally:
        start_call_session()

