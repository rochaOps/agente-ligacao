import json
import os
import tempfile
import wave
import logging

import httpx
import numpy as np

from config import VOICEVOX_URL, VOICEVOX_SPEAKER_ID, SAMPLE_RATE, TMP_DIR

logger = logging.getLogger(__name__)

_VOICEVOX_PARAMS = {
    "speedScale":       0.85,
    "pitchScale":       0.0,
    "intonationScale":  0.8,
    "volumeScale":      1.0,
    "prePhonemeLength": 0.1,
    "postPhonemeLength": 0.1,
}


def _generate_beep(sample_rate: int) -> np.ndarray:
    """880 Hz / 150 ms soft beep — signals the attendant it's their turn to speak."""
    duration  = 0.15
    n         = int(sample_rate * duration)
    t         = np.linspace(0, duration, n, endpoint=False)
    beep      = np.sin(2 * np.pi * 880 * t) * 0.10
    fade      = int(sample_rate * 0.02)
    beep[:fade]  *= np.linspace(0, 1, fade)
    beep[-fade:] *= np.linspace(1, 0, fade)
    gap = np.zeros(int(sample_rate * 0.08))
    return np.concatenate([gap, beep])


async def text_to_speech(text: str, beep: bool = False) -> str:
    """Synthesise speech via VOICEVOX. Returns path to a WAV file in TMP_DIR."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": VOICEVOX_SPEAKER_ID},
        )
        resp.raise_for_status()
        query = {**resp.json(), **_VOICEVOX_PARAMS}

        resp = await client.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": VOICEVOX_SPEAKER_ID},
            headers={"Content-Type": "application/json"},
            content=json.dumps(query),
            timeout=30.0,
        )
        resp.raise_for_status()

    raw_wav = resp.content

    if not beep:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
        tmp.write(raw_wav)
        tmp.close()
        logger.debug(f"TTS: {len(text)} chars → {tmp.name}")
        return tmp.name

    # --- append beep --------------------------------------------------------
    tmp_in = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
    tmp_in.write(raw_wav)
    tmp_in.close()

    out_path: str | None = None
    try:
        with wave.open(tmp_in.name, 'rb') as wf:
            src_rate = wf.getframerate()
            audio_np = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

        beep_np = (_generate_beep(src_rate) * np.iinfo(np.int16).max).clip(
            np.iinfo(np.int16).min, np.iinfo(np.int16).max
        ).astype(np.int16)
        combined = np.concatenate([audio_np, beep_np])

        out = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
        out_path = out.name
        out.close()
        with wave.open(out_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(src_rate)
            wf.writeframes(combined.tobytes())
    finally:
        os.unlink(tmp_in.name)

    logger.debug(f"TTS+beep: {len(text)} chars → {out_path}")
    return out_path
