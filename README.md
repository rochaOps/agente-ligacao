# agente-ligacao

> AI voice agent that makes phone calls autonomously via a physical GSM modem — powered by Claude, VOICEVOX TTS, and faster-whisper STT.

---

## What it does

`agente-ligacao` is a production system that places real phone calls through a SIM7600G-H GSM modem and conducts full voice conversations in Japanese using AI. It was built to handle a specific real-world problem: making business calls in Japan autonomously, without human intervention.

A user sends a phone number and call objective via Telegram (in Portuguese). The agent evaluates whether it has enough context, translates the objective to Japanese, dials via AT commands, speaks using VOICEVOX TTS, listens with faster-whisper STT, reasons with Claude, and reports back to Telegram in real time — including a full transcript and summary when the call ends.

---

## Architecture

```
Telegram Bot (PT-BR)
      │
      ▼
  FastAPI App (port 8100)
      │
      ├──► Claude API (claude-sonnet-4-6)
      │       ├── evaluate_context()   → is there enough info to call?
      │       ├── process_call_turn()  → what to say next
      │       └── generate_summary()  → post-call summary
      │
      ├──► VOICEVOX TTS (Speaker 13 — 青山龍星)
      │       └── generates wav audio per turn
      │
      ├──► faster-whisper STT (model: small, CPU)
      │       └── transcribes attendant speech → Japanese text
      │
      └──► SIM7600G-H GSM Modem
              ├── /dev/ttyGSM_at  → AT command interface (udev symlink)
              └── /dev/ttyGSM_pcm → PCM voice stream
```

### Call Flow

```
[Telegram] /ligar <number> <context>
    └─► evaluate_context() — sufficient info?
    └─► translate PT→JP
    └─► ATD<number>;  ← dial via serial AT command
    └─► activate PCM audio stream
    └─► loop:
          play TTS audio → record attendant → STT → Claude → TTS
          [hold detected] wait silently, resume when attendant returns
          [end detected]  farewell → hangup → summary → Telegram
```

---

## Key Engineering Challenges

**Hardware/software integration:** Direct serial port communication with a USB GSM modem using AT commands for call control and raw PCM for audio — no telephony framework, everything from scratch.

**USB autosuspend:** Linux kernel suspended the modem mid-call causing `Errno 5 / EIO`. Solved at two layers: udev rules disable autosuspend on device attach; app layer catches `OSError` and reconnects with exponential backoff (1s → 2s → 4s → ... → 32s cap).

**Real-time audio pipeline:** Custom `AudioManager` with `threading.Lock()` on the PCM port and `threading.Event()` for play/record synchronization. VAD (`webrtcvad`) prevents Whisper hallucinations from GSM line noise.

**Hold detection:** Modem sends no signal when placed on hold — agent detects silence streaks, pauses silently for up to 10 turns, and resumes when the attendant returns.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python async |
| LLM | Anthropic Claude (`claude-sonnet-4-6` / `claude-haiku-4-5`) |
| TTS | VOICEVOX (Speaker 13 — 青山龍星 ノーマル) |
| STT | faster-whisper (model: small, Japanese) |
| VAD | webrtcvad — voice activity detection |
| Modem | SIM7600G-H via pyserial (AT commands + PCM) |
| Bot | python-telegram-bot |
| DB | SQLite (call transcripts + summaries) |
| Infra | Docker + udev rules (Debian 12) |

---

## Project Structure

```
app/
├── main.py                  # FastAPI entrypoint + webhook router
├── config.py                # Environment validation + constants
├── core/
│   ├── agent.py             # Claude API: turn processing, translation, context eval
│   ├── tts.py               # VOICEVOX TTS client
│   └── stt.py               # faster-whisper STT
├── telephony/
│   ├── call_manager.py      # AT command interface + event loop + reconnect logic
│   └── audio.py             # PCM audio record/play with mutex
├── bot/
│   ├── handlers.py          # Telegram: /ligar, /desligar, /skip, incoming calls
│   └── bot_routes.py        # Webhook router endpoints
└── utils/
    └── db.py                # SQLite transcript storage
```

---

## Setup

```bash
cp .env.example .env
# Fill in your API keys

docker compose up -d
```

**Required environment variables:**

```env
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
AT_PORT=/dev/ttyGSM_at
AUDIO_PORT=/dev/ttyGSM_pcm
```

**Hardware requirement:** SIM7600G-H USB modem. The udev rules in the repo create stable device symlinks (`/dev/ttyGSM_at`, `/dev/ttyGSM_pcm`) regardless of USB enumeration order.

---

## Status

Production system, running 24/7 on a self-hosted Linux server.

Tests: `docker exec agente-ligacao python -m pytest app/tests/ -v`

---

## Author

Luis Rocha · [github.com/rochaOps](https://github.com/rochaOps)
