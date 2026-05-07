import asyncio
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime

import pytz
from telegram import Update
from telegram.ext import Application, ContextTypes

from config import (
    MAX_CALL_TURNS, MAX_INCOMING_TURNS, MAX_RECORD_DURATION,
    CALL_ANSWER_TIMEOUT, SILENCE_STREAK_MAX, CONFIDENCE_MIN, CONFIDENCE_UNCERTAIN,
    TIMEZONE, TMP_DIR, PROFILE_PATH,
)
from core.agent import (
    translate_to_japanese, translate_to_portuguese,
    start_call_session, get_call_summary, get_full_transcript,
    process_call_turn, generate_call_summary, process_incoming_call, get_user_name,
    evaluate_context,
)
from core.stt import speech_to_text
from core.tts import text_to_speech
from telephony.audio import audio_manager
from telephony.call_manager import call_manager
from utils.db import (
    salvar_ligacao_saida, salvar_turno_transcricao, salvar_resumo,
    buscar_historico, buscar_recados,
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


def _authorized(update: Update) -> bool:
    """Return True if the message is from the authorized chat. Log rejections."""
    if update.effective_chat.id == TELEGRAM_CHAT_ID:
        return True
    logger.warning(
        f"Unauthorized access attempt from chat_id={update.effective_chat.id} "
        f"user={update.effective_user.username!r}"
    )
    return False


def extract_phone(text: str) -> str | None:
    match = re.search(r'[\d\-\+\(\)]{10,}', text.replace(" ", ""))
    return match.group(0) if match else None


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
        # Versão concisa — abre como ligação comercial padrão japonesa.
        # Alternativa com menção explícita ao AI (comentado para fallback futuro):
        # intro = (
        #     f"こちらはAIアシスタントです。{user_name}の代理でご連絡しております。"
        #     f"{text_jp}"
        # )
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
                    continue  # wav_path é None, não toca nada
                silence_streak += 1
                logger.warning(f"SILENCE: turno {turn}, streak {silence_streak}")
                if silence_streak >= SILENCE_STREAK_MAX:
                    asyncio.create_task(bot.send_message(
                        chat_id,
                        f"⚠️ Áudio difícil ({silence_streak} turnos) — mantendo ligação, pedindo para repetir..."
                    ))
                    silence_streak = 0  # reseta, continua tentando
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
        start_call_session()  # always reset history for next call


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


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text  = update.message.text
    phone = extract_phone(text)

    # Handle reply to a pending context request
    if not phone and update.effective_chat.id in _pending_context:
        pending = _pending_context.pop(update.effective_chat.id)
        if time.time() > pending["expires_at"]:
            await update.message.reply_text("⏰ Tempo esgotado. Envie o número novamente para religar.")
            return
        merged       = f"{pending['context_text']}. {text}"
        phone        = pending["phone"]
        context_text = merged
        await update.message.reply_text(f"✅ Contexto atualizado. Preparando ligação para {phone}...")
        text_jp = await translate_to_japanese(context_text)
        await update.message.reply_text(f"🇯🇵 Agente vai falar:\n\n{text_jp}")
        asyncio.create_task(execute_call(phone, context_text, text_jp, update.effective_chat.id))
        return

    if phone:
        context_text = text.replace(phone, "").strip(" ,.-:—") or "ligar"
        horario = check_business_hours()
        if horario["aviso"]:
            await update.message.reply_text(horario["aviso"])
        await update.message.reply_text(f"📞 Número: {phone}\n📋 Contexto: {context_text}")

        await update.message.reply_text("🔍 Avaliando contexto...")
        evaluation = await evaluate_context(phone, context_text)

        if not evaluation.get("sufficient", True):
            _pending_context[update.effective_chat.id] = {
                "phone":        phone,
                "context_text": context_text,
                "expires_at":   time.time() + 300,
            }
            await update.message.reply_text(
                f"💬 Para ligar com mais eficácia:\n\n{evaluation['question']}\n\n"
                "_(Responda ou envie /skip para ligar assim mesmo)_",
                parse_mode="Markdown"
            )
            return

        await update.message.reply_text("🔄 Preparando script...")
        text_jp = await translate_to_japanese(context_text)
        await update.message.reply_text(f"🇯🇵 Agente vai falar:\n\n{text_jp}")
        try:
            wav_path = await text_to_speech(text_jp)
            with open(wav_path, "rb") as f:
                await context.bot.send_voice(
                    chat_id=TELEGRAM_CHAT_ID, voice=f, caption="🔊 Prévia da voz"
                )
            os.unlink(wav_path)
        except Exception as e:
            logger.warning(f"Prévia de voz falhou (continuando): {e}")
        asyncio.create_task(execute_call(phone, context_text, text_jp, TELEGRAM_CHAT_ID))
    else:
        await update.message.reply_text("🔄 Traduzindo...")
        text_jp  = await translate_to_japanese(text)
        await update.message.reply_text(f"🇯🇵 {text_jp}")
        wav_path = await text_to_speech(text_jp)
        with open(wav_path, "rb") as f:
            await context.bot.send_voice(chat_id=TELEGRAM_CHAT_ID, voice=f)
        os.unlink(wav_path)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text("👂 Transcrevendo áudio...")
    voice = update.message.voice
    file  = await context.bot.get_file(voice.file_id)
    tmp   = tempfile.NamedTemporaryFile(suffix=".ogg", dir=TMP_DIR, delete=False)
    await file.download_to_drive(tmp.name)
    tmp.close()
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: speech_to_text(tmp.name))
    os.unlink(tmp.name)
    if result["pedir_repeticao"]:
        await update.message.reply_text(
            f"👂 Transcrição ({result['confianca']:.0%}):\n{result['texto']}\n\n⚠️ Baixa confiança."
        )
    else:
        await update.message.reply_text(f"📝 Transcrição:\n{result['texto']}")
    text_jp  = await translate_to_japanese(result["texto"])
    await update.message.reply_text(f"🇯🇵 {text_jp}")
    wav_path = await text_to_speech(text_jp)
    with open(wav_path, "rb") as f:
        await context.bot.send_voice(chat_id=TELEGRAM_CHAT_ID, voice=f)
    os.unlink(wav_path)


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text("""🤖 *Agente de Ligação JP*

*Como usar:*
📞 Com número: `052-XXX-XXXX agendar consulta médica`
💬 Só tradução: `preciso remarcar meu agendamento`

*Comandos:*
/status — status do sistema
/desligar — força encerramento da ligação ativa
/perfil — dados cadastrados
/historico — últimas ligações
/recados — recados recebidos
/fila — ligações na fila
/resumo — resumo da ligação atual
/retentar — executa fila
/limpar — limpa histórico
/skip — liga sem aguardar contexto adicional
/help — esta mensagem
""", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    tts_status = "✅ VOICEVOX TTS (japonês)"
    try:
        buscar_historico(1)
        db = "✅ Banco de dados OK"
    except Exception:
        db = "❌ Banco de dados com erro"
    sinal   = call_manager.get_signal()
    reg     = call_manager.get_registration()
    gsm     = f"✅ SIM7600 — sinal {sinal}/31 — {reg}" if sinal > 0 else "❌ SIM7600 offline"
    horario = check_business_hours()
    horario_status = "✅ Horário comercial" if horario["is_open"] else f"⚠️ {horario['dia_semana']} {horario['hora_atual']}"
    fila    = f"📋 Fila: {len(_retry_queue)} ligação(ões)" if _retry_queue else "📋 Fila vazia"
    await update.message.reply_text(f"""⚙️ *Status do Sistema*

{tts_status}
✅ Whisper small int8 carregado
✅ Claude API configurada
{gsm}
{db}
{horario_status}
{fila}
🌐 Modo: 🇯🇵 Japonês
""", parse_mode="Markdown")


async def cmd_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    import json
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            profile = json.load(f)
        endereco = profile.get("endereco", {})
        seguro   = profile.get("seguro_saude", {})
        await update.message.reply_text(f"""👤 *Perfil Cadastrado*

Nome: {profile.get('nome_japones')} ({profile.get('nome_romaji')})
Nascimento: {profile.get('data_nascimento')}
Celular: {profile.get('telefone_celular')}

📍 〒{endereco.get('cep')} {endereco.get('prefeitura')} {endereco.get('cidade')}

🏥 {seguro.get('seguradora')} — válido até {seguro.get('validade')}
""", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao ler perfil: {e}")


async def cmd_historico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    rows = buscar_historico(5)
    if not rows:
        await update.message.reply_text("📭 Nenhuma ligação registrada ainda.")
        return
    msg = "📋 *Últimas ligações:*\n\n"
    for data_hora, numero, contexto, status, resultado in rows:
        msg += f"📞 `{numero}`\n🕐 {data_hora}\n📋 {contexto}\nStatus: {status}\n"
        if resultado:
            msg += f"Resultado: {resultado}\n"
        msg += "---\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_recados(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    rows = buscar_recados(5)
    if not rows:
        await update.message.reply_text("📭 Nenhum recado recebido ainda.")
        return
    msg = "📬 *Últimos recados:*\n\n"
    for data_hora, numero_origem, resumo, status in rows:
        msg += f"📞 De: `{numero_origem or 'desconhecido'}`\n🕐 {data_hora}\n📋 {resumo}\nStatus: {status}\n---\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_fila(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not _retry_queue:
        await update.message.reply_text("📭 Fila vazia.")
        return
    msg = "📋 *Fila de ligações:*\n\n"
    for i, item in enumerate(_retry_queue, 1):
        msg += f"{i}. 📞 `{item['numero']}`\n📋 {item['contexto']}\n---\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        f"📝 *Resumo:*\n\n{get_call_summary()}", parse_mode="Markdown"
    )


async def cmd_retentar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not _retry_queue:
        await update.message.reply_text("📭 Fila vazia.")
        return
    item    = _retry_queue.pop(0)
    text_jp = await translate_to_japanese(item["contexto"])
    asyncio.create_task(execute_call(item["numero"], item["contexto"], text_jp, TELEGRAM_CHAT_ID))


async def cmd_desligar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    loop  = asyncio.get_running_loop()
    clcc  = await loop.run_in_executor(None, lambda: call_manager._send_at("AT+CLCC", wait=1.0))
    if "+CLCC:" not in clcc:
        await update.message.reply_text("ℹ️ Nenhuma chamada ativa no módulo.")
        call_manager.call_active = False
        return
    await update.message.reply_text("📵 Encerrando ligação...")
    audio_manager.stop_playback()
    raw = await loop.run_in_executor(None, lambda: call_manager._send_at("AT+CHUP", wait=3.0))
    call_manager.call_active = False
    logger.info(f"/desligar AT+CHUP → {raw.strip()!r}")
    if "VOICE CALL: END" in raw:
        await update.message.reply_text("✅ Ligação encerrada.")
    elif "OK" in raw:
        await update.message.reply_text(
            f"⚠️ Módulo respondeu OK mas sem confirmação.\n`{raw.strip()}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ Falhou: `{raw.strip()!r}`", parse_mode="Markdown")


async def cmd_limpar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    try:
        from config import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("DELETE FROM ligacoes_saida")
        c.execute("DELETE FROM ligacoes_recebidas")
        conn.commit()
        conn.close()
        _retry_queue.clear()
        await update.message.reply_text("🗑️ Histórico e fila limpos.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in _pending_context:
        await update.message.reply_text("ℹ️ Nenhuma ligação aguardando contexto.")
        return
    pending = _pending_context.pop(chat_id)
    await update.message.reply_text("▶️ Ligando sem contexto adicional...")
    text_jp = await translate_to_japanese(pending["context_text"])
    asyncio.create_task(execute_call(pending["phone"], pending["context_text"], text_jp, chat_id))


async def cmd_transcricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    from utils.db import buscar_resumos
    rows = buscar_resumos(3)
    if not rows:
        await update.message.reply_text("📭 Nenhuma transcrição disponível.")
        return
    for ligacao_id, numero, data_hora, duracao_turnos, resumo_pt in rows:
        msg = (
            f"📞 *Ligação #{ligacao_id}*\nNúmero: `{numero}`\n"
            f"Data: {data_hora}\nTurnos: {duracao_turnos}\n\n*Resumo:*\n{resumo_pt}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
