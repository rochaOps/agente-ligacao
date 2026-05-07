import math
import os
import tempfile
import wave
import logging

import numpy as np
import soxr
from faster_whisper import WhisperModel

from config import (
    SAMPLE_RATE, SAMPLE_WIDTH, TMP_DIR,
    WHISPER_MODEL, WHISPER_THREADS, WHISPER_COMPUTE_TYPE,
    CONFIDENCE_UNCERTAIN,
)

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None


def load_model() -> None:
    global _model
    logger.info(f"Carregando faster-whisper {WHISPER_MODEL} {WHISPER_COMPUTE_TYPE}...")
    _model = WhisperModel(WHISPER_MODEL, device="cpu",
                          compute_type=WHISPER_COMPUTE_TYPE, cpu_threads=WHISPER_THREADS)
    logger.info("faster-whisper carregado!")


def _normalize_to_16k(audio_path: str) -> str:
    # Non-WAV formats (ogg, mp3, etc.) go through ffmpeg then resample if needed
    try:
        with wave.open(audio_path, 'rb') as wf:
            src_rate     = wf.getframerate()
            src_channels = wf.getnchannels()
            src_width    = wf.getsampwidth()
    except wave.Error:
        import subprocess
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
        tmp.close()
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", str(SAMPLE_RATE),
             "-ac", "1", "-sample_fmt", "s16", tmp.name],
            capture_output=True, check=True,
        )
        return tmp.name

    if src_rate == SAMPLE_RATE and src_channels == 1:
        return audio_path

    with wave.open(audio_path, 'rb') as wf:
        raw = wf.readframes(wf.getnframes())

    dtype    = np.int16 if src_width == 2 else np.int8
    audio_np = np.frombuffer(raw, dtype=dtype)

    if src_channels == 2:
        audio_np = audio_np.reshape(-1, 2).mean(axis=1).astype(dtype)

    if src_rate != SAMPLE_RATE:
        audio_float = audio_np.astype(np.float32) / np.iinfo(np.int16).max
        resampled   = soxr.resample(audio_float, src_rate, SAMPLE_RATE, quality='VHQ')
        audio_np    = (resampled * np.iinfo(np.int16).max).clip(
            np.iinfo(np.int16).min, np.iinfo(np.int16).max
        ).astype(np.int16)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
    tmp_name = tmp.name
    tmp.close()
    try:
        with wave.open(tmp_name, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_np.tobytes())
    except Exception:
        os.unlink(tmp_name)
        raise
    return tmp_name


def speech_to_text(audio_path: str, min_confidence: float = CONFIDENCE_UNCERTAIN) -> dict:
    global _model
    if _model is None:
        load_model()

    converted_path = _normalize_to_16k(audio_path)
    try:
        segments, _ = _model.transcribe(
            converted_path,
            language="ja",
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt="ありがとうございます。どのようなご用件でしょうか。少々お待ちください。",
        )
        segments = list(segments)
    finally:
        if converted_path != audio_path:
            os.unlink(converted_path)

    if not segments:
        return {"texto": "", "confianca": 0.0, "pedir_repeticao": True}

    texto      = " ".join(s.text.strip() for s in segments).strip()
    avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
    confidence  = max(0.0, min(1.0, math.exp(avg_logprob)))

    logger.info(f"STT: '{texto}' | confiança: {confidence:.2f}")

    return {
        "texto":          texto,
        "confianca":      confidence,
        "pedir_repeticao": confidence < min_confidence,
    }
