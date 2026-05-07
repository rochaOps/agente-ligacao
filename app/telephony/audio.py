import asyncio
import logging
import os
import tempfile
import threading
import time
import wave
from typing import Optional

import numpy as np
import soxr
import webrtcvad

from config import AUDIO_PORT, SAMPLE_RATE, SAMPLE_WIDTH, CHANNELS, TMP_DIR

logger = logging.getLogger(__name__)

_FRAME_MS   = 30                                              # WebRTC VAD requires 10/20/30ms frames
_FRAME_SIZE = int(SAMPLE_RATE * SAMPLE_WIDTH * _FRAME_MS / 1000)  # 960 bytes @ 16kHz 16-bit


class AudioManager:
    def __init__(self) -> None:
        self._play_event         = threading.Event()          # M2: set=playing, clear=stopped
        self._port_lock          = threading.Lock()           # M1: exclusive PCM port access
        self._play_thread:       Optional[threading.Thread] = None
        self._barge_in_frames:   list[bytes] = []
        self._barge_in_detected: bool = False

    # ── Port ──────────────────────────────────────────────────────────────────

    def _open_audio_port(self):
        try:
            import serial
            port = serial.Serial(port=AUDIO_PORT, baudrate=115200, timeout=0.1)
            logger.info(f"Porta de áudio {AUDIO_PORT} aberta")
            return port
        except Exception as e:
            logger.error(f"Erro ao abrir porta de áudio: {e}")
            return None

    # ── Playback ──────────────────────────────────────────────────────────────

    def play_audio(self, wav_path: str) -> bool:
        self._play_event.set()                                # M2
        self._play_thread = threading.Thread(
            target=self._play_loop, args=(wav_path,), daemon=True
        )
        self._play_thread.start()
        return True

    def stop_playback(self) -> None:
        self._play_event.clear()                              # M2

    def stop_recording(self) -> None:
        pass  # VAD loop exits when call_active is False

    def wait_playback(self, timeout: float = 30.0) -> None:
        self._play_event.wait(timeout=timeout)                # M2: blocks until clear()
        if self._play_thread:
            self._play_thread.join(timeout=timeout)

    def _play_loop(self, wav_path: str) -> None:
        with self._port_lock:                                 # M1: exclusive port ownership
            port = self._open_audio_port()
            if not port:
                self._play_event.clear()                      # M2
                return
            self._barge_in_frames    = []
            self._barge_in_detected  = False
            try:
                with wave.open(wav_path, 'rb') as wf:
                    src_rate     = wf.getframerate()
                    src_width    = wf.getsampwidth()
                    src_channels = wf.getnchannels()
                    raw          = wf.readframes(wf.getnframes())

                logger.info(f"Reproduzindo {wav_path}: {src_rate}Hz/{src_width*8}bit/{src_channels}ch")

                dtype    = np.int16 if src_width == 2 else np.int8
                audio_np = np.frombuffer(raw, dtype=dtype)

                if src_channels == 2:
                    audio_np = audio_np.reshape(-1, 2).mean(axis=1).astype(dtype)

                if src_rate != SAMPLE_RATE:
                    audio_float = audio_np.astype(np.float32) / np.iinfo(dtype).max
                    resampled   = soxr.resample(audio_float, src_rate, SAMPLE_RATE, quality='VHQ')
                    audio_np    = (resampled * np.iinfo(np.int16).max).clip(
                        np.iinfo(np.int16).min, np.iinfo(np.int16).max
                    ).astype(np.int16)

                # Noise gate: silence samples below 1% of peak to remove background hiss
                peak      = np.abs(audio_np).max()
                threshold = int(peak * 0.01)
                audio_np  = np.where(np.abs(audio_np) < threshold, 0, audio_np).astype(np.int16)
                raw       = audio_np.tobytes()

                block      = int(SAMPLE_RATE * SAMPLE_WIDTH * 0.02)  # 640 bytes = 20ms
                offset     = 0
                start_time = time.monotonic()
                while self._play_event.is_set() and offset < len(raw):  # M2
                    chunk   = raw[offset: offset + block]
                    port.write(chunk)
                    offset += block
                    expected  = start_time + offset / (SAMPLE_RATE * SAMPLE_WIDTH)
                    remaining = expected - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
            except Exception as e:
                logger.error(f"Erro na reprodução: {e}")
            finally:
                self._play_event.clear()                      # M2
                port.close()
                logger.info("Reprodução encerrada")

    async def play_and_wait(self, wav_path: str) -> None:
        self.play_audio(wav_path)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._play_thread.join(timeout=30.0) if self._play_thread else None,
        )

    # ── Recording ─────────────────────────────────────────────────────────────

    async def record_turn(self, duration: float = 8.0) -> tuple[str, bool]:
        loop = asyncio.get_running_loop()
        tmp  = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
        tmp.close()
        heard = await loop.run_in_executor(None, lambda: self._record_with_vad(tmp.name, duration))
        return tmp.name, heard

    def _record_with_vad(self, output_path: str, max_duration: float = 8.0,
                         aggressiveness: int = 1,
                         min_speech_duration: float = 0.3,
                         silence_cutoff: float = 0.3) -> bool:
        """
        Record using WebRTC VAD. Stops after silence_cutoff seconds of silence
        following at least min_speech_duration seconds of detected speech.
        Exits immediately if call_active becomes False (hangup detected).
        Returns True if VAD confirmed speech above min_speech_duration threshold;
        False if the recording window elapsed with only noise/silence frames.
        """
        from telephony.call_manager import call_manager

        vad            = webrtcvad.Vad(aggressiveness)
        max_frames     = int(max_duration * 1000 / _FRAME_MS)
        silence_needed = int(silence_cutoff * 1000 / _FRAME_MS)
        speech_needed  = int(min_speech_duration * 1000 / _FRAME_MS)

        with self._port_lock:                                 # M1: exclusive port ownership
            port = self._open_audio_port()
            if not port:
                _write_empty_wav(output_path)
                return False

            time.sleep(0.15)           # wait for PCM stream to stabilize after port open
            port.reset_input_buffer()

            frames        = list(self._barge_in_frames)
            self._barge_in_frames = []
            heard_speech  = len(frames) >= speech_needed
            speech_count  = min(len(frames), speech_needed)
            silence_count = 0

            try:
                for frame_idx in range(max_frames):
                    if not call_manager.call_active:
                        logger.info("VAD: ligação encerrada — interrompendo gravação")
                        break

                    buf      = b""
                    deadline_mult = 16 if frame_idx < 5 else 3
                    deadline = time.time() + _FRAME_MS / 1000 * deadline_mult
                    while len(buf) < _FRAME_SIZE and time.time() < deadline:
                        if port.in_waiting:
                            buf += port.read(min(port.in_waiting, _FRAME_SIZE - len(buf)))
                        else:
                            time.sleep(0.003)

                    if len(buf) < _FRAME_SIZE:
                        silence_count += 1
                        continue

                    frames.append(buf)
                    try:
                        is_speech = vad.is_speech(buf, SAMPLE_RATE)
                    except Exception:
                        is_speech = False

                    if is_speech:
                        speech_count  += 1
                        silence_count  = 0
                        if speech_count >= speech_needed:
                            heard_speech = True
                    else:
                        silence_count += 1
                        speech_count   = 0

                    if speech_count == 1 or (heard_speech and silence_count == 1):
                        logger.debug(f"VAD sc={speech_count} sil={silence_count} heard={heard_speech}")

                    if heard_speech and silence_count >= silence_needed:
                        logger.info(f"VAD: silêncio após {len(frames) * _FRAME_MS / 1000:.1f}s")
                        break
            except Exception as e:
                logger.error(f"Erro no VAD: {e}")
            finally:
                port.close()

        with wave.open(output_path, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))

        logger.info(f"VAD: gravados {len(frames) * _FRAME_MS / 1000:.1f}s → {output_path} heard={heard_speech}")
        return heard_speech


def _write_empty_wav(path: str) -> None:
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)


audio_manager = AudioManager()
