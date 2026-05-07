import os
import re
import serial
import threading
import time
import logging
from typing import Callable, Optional

import errno as _errno
from config import AT_PORT, BAUD_RATE

logger = logging.getLogger(__name__)

# Only digits, +, and leading whitespace — no control characters that could inject commands
_PHONE_RE = re.compile(r'^[\d\+]{7,15}$')


def sanitize_phone(number: str) -> str:
    """Strip spaces/dashes, then validate against E.164-ish pattern."""
    cleaned = re.sub(r'[\s\-\(\)]', '', number)
    if not _PHONE_RE.match(cleaned):
        raise ValueError(f"Número de telefone inválido: {number!r}")
    return cleaned


class CallManager:
    def __init__(self) -> None:
        self.ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False
        self.on_ring: Optional[Callable[[str], None]] = None
        self.on_call_begin: Optional[Callable[[], None]] = None
        self.on_call_end: Optional[Callable[[], None]] = None
        self.on_dtmf: Optional[Callable[[str], None]] = None
        self.call_active = False
        self.incoming_number: Optional[str] = None
        self.call_end_reason: Optional[str] = None
        self.call_failed: bool = False
        self.call_fail_reason: Optional[str] = None

    def connect(self) -> bool:
        try:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = serial.Serial(
                port=AT_PORT,
                baudrate=BAUD_RATE,
                timeout=1,
                exclusive=True
            )
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            logger.info(f"Porta {AT_PORT} aberta com sucesso")
            self._start_listener()
            return True
        except Exception as e:
            logger.error(f"Erro ao abrir porta serial: {e}")
            return False

    def disconnect(self) -> None:
        self._running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("Porta serial fechada")

    def _send_at(self, cmd: str, wait: float = 1.0) -> str:
        with self._lock:
            if not self.ser or not self.ser.is_open:
                logger.warning(f"_send_at({cmd}): porta serial fechada — reconectando...")
                self.connect()
                if not self.ser or not self.ser.is_open:
                    logger.error(f"_send_at({cmd}): falha ao reconectar")
                    return ""
            self._paused = True
            try:
                self.ser.reset_input_buffer()
                self.ser.write(f"{cmd}\r\n".encode())
                time.sleep(wait)
                response = ""
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if self.ser.in_waiting:
                        response += self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                        time.sleep(0.1)
                    else:
                        break
                logger.debug(f"AT << {cmd} >> {response.strip()!r}")
                self._dispatch_events_from_response(response)
                return response
            finally:
                self._paused = False

    _UNSOLICITED_PREFIXES = (
        "RING", "+CLIP:", "VOICE CALL:", "NO CARRIER", "BUSY", "+DTMF:",
    )
    # Events that mutate call state — suppressed when reading AT command responses
    # to avoid false hangup from NO CARRIER echoed in response buffers.
    _STATE_CHANGING_EVENTS = ("VOICE CALL: END", "NO CARRIER")

    def _dispatch_events_from_response(self, response: str) -> None:
        for line in response.split("\r\n"):
            line = line.strip()
            if not line:
                continue
            if any(line.startswith(p) or p in line for p in self._UNSOLICITED_PREFIXES):
                if any(ev in line for ev in self._STATE_CHANGING_EVENTS):
                    logger.debug(f"EVENT (from AT response, suppressed): {line}")
                    continue
                logger.debug(f"EVENT (from AT response): {line}")
                self._handle_event(line)

    def _start_listener(self) -> None:
        self._running = True
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()

    def _listen_loop(self) -> None:
        buffer = ""
        while self._running:
            try:
                if self._paused:
                    time.sleep(0.05)
                    continue
                if self.ser and self.ser.is_open and self.ser.in_waiting:
                    data = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                    buffer += data
                    lines = buffer.split("\r\n")
                    buffer = lines[-1]
                    for line in lines[:-1]:
                        line = line.strip()
                        if not line:
                            continue
                        logger.debug(f"EVENT: {line}")
                        self._handle_event(line)
                else:
                    time.sleep(0.05)
            except OSError as e:
                if e.errno == _errno.EIO:
                    logger.warning("Modem desconectado (Errno 5) — aguardando reconexão...")
                    self._reconnect_with_backoff()
                    buffer = ""
                else:
                    logger.error(f"Erro no listener: {e}")
                    time.sleep(1)
            except Exception as e:
                logger.error(f"Erro no listener: {e}")
                time.sleep(1)

    def _reconnect_with_backoff(self, max_attempts: int = 5, base_delay: float = 1.0) -> None:
        """Reconnect with exponential backoff (1s, 2s, 4s, 8s, 16s, capped at 32s).

        Handles USB device re-enumeration after autosuspend or modem reset.
        - base_delay: Initial wait time in seconds (default 1.0)
        - max_attempts: Max reconnect attempts (default 5, total ~62s max)

        After successful reconnect, backoff resets to 1s for next cycle.
        """
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self._running = False

        for attempt in range(1, max_attempts + 1):
            # Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 32s
            delay = min(base_delay * (2 ** (attempt - 1)), 32.0)
            logger.debug(f"Reconnect attempt {attempt}/{max_attempts}: waiting {delay:.0f}s for device...")
            time.sleep(delay)

            # Verify device symlink exists before attempting connect
            if not os.path.exists(AT_PORT):
                logger.debug(f"Device {AT_PORT} not yet available (attempt {attempt})")
                continue

            # Attempt reconnect
            self._running = True
            if self.connect():
                logger.info(f"✓ Modem reconnected successfully after {attempt} attempt(s)")
                return
            self._running = False

        logger.error(f"✗ Reconnect failed after {max_attempts} attempts ({(max_attempts-1)*32:.0f}s timeout) — modem unavailable")

    def _handle_event(self, line: str) -> None:
        if line.startswith("RING"):
            logger.info("📞 Chamada recebida!")
            if self.on_ring:
                self.on_ring(self.incoming_number or "desconhecido")
        elif line.startswith("+CLIP:"):
            try:
                number = line.split('"')[1]
                self.incoming_number = number
                logger.info(f"📞 Número identificado: {number}")
            except (IndexError, ValueError):
                logger.warning(f"+CLIP parse falhou: {line!r}")
        elif "VOICE CALL: BEGIN" in line:
            self.call_active = True
            logger.info("✅ Ligação conectada — VOICE CALL BEGIN")
            if self.on_call_begin:
                self.on_call_begin()
        elif "VOICE CALL: END" in line or ("NO CARRIER" in line and self.call_active):
            if not self.call_end_reason:
                self.call_end_reason = "remote_hangup"
            self.call_active = False
            logger.warning(f"📵 LIGAÇÃO ENCERRADA pelo lado remoto — razão: {self.call_end_reason}")
            if self.on_call_end:
                self.on_call_end()
        elif "NO CARRIER" in line and not self.call_active:
            self.call_failed = True
            self.call_fail_reason = "no_carrier"
            logger.warning("NO CARRIER antes de VOICE CALL: BEGIN — linha fora de serviço ou não atendeu")
        elif "BUSY" in line and not self.call_active:
            self.call_failed = True
            self.call_fail_reason = "busy"
            logger.warning("BUSY recebido — linha ocupada")
        elif line.startswith("+DTMF:"):
            digit = line.split(":")[1].strip()
            logger.info(f"🔢 DTMF: {digit}")
            if self.on_dtmf:
                self.on_dtmf(digit)

    def dial(self, number: str) -> bool:
        self.call_failed = False
        self.call_fail_reason = None
        try:
            number = sanitize_phone(number)
        except ValueError as e:
            logger.error(f"dial: {e}")
            return False
        logger.info(f"📞 Discando para {number}")
        response = self._send_at(f"ATD{number};", wait=3.0)
        if "OK" in response and "ERROR" not in response:
            return True
        logger.error(f"Erro ao discar: {response!r}")
        return False

    def answer(self) -> bool:
        response = self._send_at("ATA", wait=2.0)
        return "OK" in response

    def hangup(self) -> bool:
        # AT+CHUP confirmed to send VOICE CALL: END on SIM7600. ATH does not.
        if not self.call_end_reason:
            self.call_end_reason = "agent_hangup"
        resp = self._send_at("AT+CHUP", wait=3.0)
        logger.info(f"hangup AT+CHUP → {resp.strip()!r}")
        success = "VOICE CALL: END" in resp or "OK" in resp
        if success:
            self.call_active = False
        return success

    def enable_pcm_audio(self) -> bool:
        t0 = time.monotonic()
        logger.info("enable_pcm_audio: start")
        self._send_at("AT+CPCMFRM=1", wait=0.15)  # 16kHz — must be set BEFORE CPCMREG=1
        self._send_at("AT+CPCMREG=1", wait=0.5)   # enable PCM USB — needs 500ms to init stream
        self._send_at("AT+CECM=1",    wait=0.15)  # hardware echo cancellation
        self._send_at("AT+CMICGAIN=5", wait=0.1)  # mic gain
        self._send_at("AT+COUTGAIN=8", wait=0.1)  # speaker gain (max)
        logger.info(f"enable_pcm_audio: done in {time.monotonic() - t0:.3f}s")
        return True

    def disable_pcm_audio(self) -> bool:
        response = self._send_at("AT+CPCMREG=0,1", wait=1.0)
        return "OK" in response

    def enable_clip(self) -> bool:
        response = self._send_at("AT+CLIP=1", wait=1.0)
        return "OK" in response

    def get_signal(self) -> int:
        response = self._send_at("AT+CSQ", wait=1.0)
        try:
            return int(response.split("+CSQ:")[1].split(",")[0].strip())
        except (IndexError, ValueError):
            return -1

    def get_registration(self) -> str:
        response = self._send_at("AT+CEREG?", wait=1.0)
        try:
            return response.split("+CEREG:")[1].split("\r")[0].strip()
        except (IndexError, ValueError):
            return "desconhecido"

    def initialize(self) -> bool:
        if not self.connect():
            return False
        time.sleep(2)
        resp = self._send_at("AT", wait=1.0)
        if "OK" not in resp:
            logger.error(f"Módulo não responde ao AT básico: {resp!r}")
            return False
        self._send_at("AT+CMEE=2", wait=1.0)
        self._send_at("AT+CVHU=0", wait=1.0)  # required for ATH to work in voice calls
        self.enable_clip()
        time.sleep(1)
        sinal = self.get_signal()
        reg   = self.get_registration()
        logger.info(f"SIM7600 inicializado — sinal: {sinal}/31 — registro: {reg}")
        return True


call_manager = CallManager()
