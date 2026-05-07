# AI Phone Agent — Technical Architecture

**Version:** 1.0  
**Date:** April 2026  
**Author:** Luis Rocha  
**Platform:** Self-hosted Linux server / SIM7600G-H

---

## Table of Contents

1. [What this system does](#1-what-this-system-does)
2. [How it works — overview](#2-how-it-works--overview)
3. [Infrastructure](#3-infrastructure)
4. [File structure](#4-file-structure)
5. [Entry point — main.py](#5-entry-point--mainpy)
6. [Intelligence core — core/](#6-intelligence-core--core)
   - 6.1 [agent.py — Call brain](#61-agentpy--call-brain)
   - 6.2 [stt.py — Ears](#62-sttpy--ears)
   - 6.3 [tts.py — Voice](#63-ttspy--voice)
   - 6.4 [call_context.py — Per-call language memory](#64-call_contextpy--per-call-language-memory)
7. [Telegram interface — bot/handlers.py](#7-telegram-interface--bothandlerspy)
8. [Telephony — telephony/](#8-telephony--telephony)
   - 8.1 [call_manager.py — GSM module communication](#81-call_managerpy--gsm-module-communication)
   - 8.2 [audio.py — Audio recording and playback](#82-audiopy--audio-recording-and-playback)
9. [Database — utils/db.py](#9-database--utilsdbpy)
10. [Environment variables](#10-environment-variables)
11. [Full call flow](#11-full-call-flow)
12. [Language system](#12-language-system)
13. [Error handling and edge cases](#13-error-handling-and-edge-cases)
14. [Telegram commands](#14-telegram-commands)
15. [Known limits](#15-known-limits)
16. [Glossary](#16-glossary)

---

## 1. What this system does

This is an **AI-powered automated phone agent**. It allows a user to instruct the system via Telegram to call a phone number and conduct a full voice conversation in Portuguese or Japanese, completely autonomously.

### What it does

- Receives instructions via Telegram (e.g., "call 052-XXXX-XXXX and schedule a medical appointment")
- Dials using a physical GSM chip installed on the server
- Speaks to the attendant using synthesized voice (Text-to-Speech)
- Listens to the attendant's response and transcribes it (Speech-to-Text)
- Sends the transcription to the AI (Claude) which decides what to say next
- Continues the conversation until the goal is met or the call ends
- Sends a call summary back to Telegram

### What it does **not** do

- Does not use VoIP or internet for calls — uses a real physical SIM chip
- Does not depend on external TTS/STT services — everything runs locally
- Does not call without user authorization — every call is initiated manually via Telegram

---

## 2. How it works — overview

Think of it as an employee who:
1. Receives a task on Telegram
2. Calls the number using a physical phone
3. Speaks to the attendant using a script
4. Listens to the attendant's response
5. Reasons about the right reply
6. Responds and continues the conversation
7. Hangs up and sends you a summary

Each step has a dedicated component:

```
YOU (Telegram)
      │
      ▼
[bot/handlers.py]  ← receives your messages and coordinates everything
      │
      ├──► [telephony/call_manager.py]  ← places the call via GSM chip
      │
      ├──► [core/tts.py]  ← converts text to voice
      │
      ├──► [telephony/audio.py]  ← sends/receives audio through the chip
      │
      ├──► [core/stt.py]  ← converts recorded audio to text
      │
      └──► [core/agent.py]  ← decides what to say (Claude AI)
```

---

## 3. Infrastructure

The system runs on a self-hosted Linux server with Docker.

| Component | Description |
|-----------|-------------|
| GSM module | SIM7600G-H (connected via USB) |
| Runtime | Docker + Python 3.12 |
| AI API | Anthropic Claude API |

### GSM module serial ports

The SIM7600G-H exposes two device files to the OS:

- `/dev/ttyUSB2` — **AT command** channel (control: dial, hang up, check signal)
- `/dev/ttyUSB4` — **PCM audio** channel (real-time voice data during a call)

> **Why two ports?** The GSM module separates control from data — like having a remote control (USB2) and an audio cable (USB4) as two distinct channels for the same device.

### Software services

| Service | Location | Purpose |
|---------|----------|---------|
| FastAPI | Main server | Agent API server |
| VOICEVOX | Local Docker container | Japanese speech synthesis |
| SQLite | Local file `/data/historico.db` | Call history database |
| Piper TTS | In-memory model | Portuguese speech synthesis |
| faster-whisper | In-memory model | Speech transcription |

---

## 4. File structure

```
/app/
├── main.py                    # Entry point — starts everything
├── core/
│   ├── agent.py               # AI (Claude) — reasoning and responses
│   ├── stt.py                 # Speech-to-Text — listen and transcribe
│   ├── tts.py                 # Text-to-Speech — generate voice
│   └── call_context.py        # Per-call language isolation
├── bot/
│   └── handlers.py            # Telegram interface — commands and call logic
├── telephony/
│   ├── call_manager.py        # GSM chip control via AT commands
│   ├── audio.py               # PCM audio recording and playback
│   └── adb_handler.py         # GSM status utility
├── utils/
│   └── db.py                  # SQLite database
└── log_config.json            # Logging configuration

/config/
└── user_profile.json          # User profile data (name, address, etc.) — not committed

/data/
└── historico.db               # SQLite database (auto-created)
```

---

## 5. Entry point — main.py

**File:** `/app/main.py`

This is the system's entry point. When the server starts (via Docker), this file runs first.

### Startup sequence

```
1. Initialize database (create tables if they don't exist)
2. Load Whisper model (STT) into memory — ~10 seconds
3. Load Piper model (Portuguese TTS) into memory — ~3 seconds
4. Initialize GSM module (SIM7600G-H) — check signal and registration
5. Start Telegram bot (begin listening for messages)
```

> **Why pre-load models?** AI models are large files. Loading them mid-call would cause 10+ second delays. Pre-loading at startup keeps them in RAM ready for immediate use.

### Available endpoints

| Route | Function |
|-------|----------|
| `GET /health` | Server liveness check |

---

## 6. Intelligence core — core/

### 6.1 agent.py — Call brain

**File:** `/app/core/agent.py`

This module handles all communication with Claude (Anthropic AI) and maintains the conversation history during a call.

#### User profile

At startup, the system reads `/config/user_profile.json` containing the user's personal data (name, address, health plan, etc.). The AI uses these when the attendant asks for information. **These are never listed to the attendant** — the AI uses them only when necessary to advance the conversation.

#### System prompts

Two sets of instructions are created for the AI, one per language:

**Portuguese mode** — instructs the AI to:
- Always respond in Brazilian Portuguese
- Use at most 2 short sentences per response
- Be direct as in a real phone call
- Never reveal or list registered personal data
- Detect when the attendant is transferring (`[TRANSFERINDO]`)
- Detect when the attendant asked to hold (`[AGUARDANDO]`)
- Detect farewells and respond with `[ENCERRAR]`

**Japanese mode** — instructs the AI to:
- Use formal Japanese (keigo — respectful language)
- Respond in 1 to 2 short sentences
- Use profile data when necessary
- Detect transfer (`[転送]`), hold (`[保留]`) and closing (`[終了]`) events

#### Conversation history

During each call, the system maintains a history of the last **20 turns**. This history is sent to the AI with each response so it understands prior context.

#### Uncertain transcription handling

When STT transcribes audio with low confidence (between 20% and 55%), the system sends the uncertain transcription to the AI with a special instruction:

```
"Try to infer what was said based on this call's context.
 Formulate a natural confirmation question in 1 short sentence."
```

The AI reasons about what was likely said and asks the attendant to confirm. For example, if STT captured "cps", the AI might ask "Did you ask for my CPF number?"

#### Claude API parameters

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model | `claude-haiku-4-5-20251001` | Fastest and cheapest for short responses |
| Max tokens | 150 | Short phone-call responses (1-2 sentences) |
| Timeout | 15 seconds | Prevents call stalling if API is slow |
| History | Last 20 turns | Sufficient context without excessive cost |

---

### 6.2 stt.py — Ears

**File:** `/app/core/stt.py`

Converts audio recorded during the call to text (Speech-to-Text). Uses **faster-whisper**, running locally on CPU.

#### Model

```
Whisper "small" — int8 — 8 CPU threads
```

**Why "small" and not smaller?** The `tiny` and `base` Whisper models make serious errors in Portuguese. In testing, "Meu nome é Luis Rocha" became "Eu não me aluiço, roxa". The `small` model has acceptable quality.

**Why int8?** A compression technique that reduces memory usage and speeds up processing with minimal quality loss.

**Why 8 threads?** Using 8 threads for Whisper significantly reduces transcription time compared to the default.

#### Known Whisper limitation

Whisper always processes a **30-second** audio window internally, even if the recorded audio is 2 seconds. This is an architectural characteristic of the model and **cannot be worked around without switching models**. STT always takes ~3 seconds regardless of audio length.

#### Audio normalization

Before transcribing, the system verifies the audio is in the correct format:
- **Sample rate:** 16,000 Hz
- **Channels:** Mono

If the audio arrived in a different format, the system converts it automatically using `soxr` at Very High Quality (VHQ).

#### Return value

```python
{
    "texto":           "what the attendant said",
    "confianca":       0.85,      # 0.0 to 1.0
    "pedir_repeticao": False      # True if confidence < 50%
}
```

---

### 6.3 tts.py — Voice

**File:** `/app/core/tts.py`

Converts text to voice audio (Text-to-Speech). Uses different engines depending on the call language.

#### Portuguese engine — Piper TTS

**Piper** is an open-source neural TTS system running completely offline. Model used:

```
pt_BR-faber-medium.onnx  — Brazilian male voice, medium quality
```

- Latency: ~200ms for short sentences
- No internet dependency
- Generates 22,050 Hz audio, resampled to 16,000 Hz for the pipeline

#### Japanese engine — VOICEVOX

**VOICEVOX** is a Japanese TTS system running in a local Docker container. Configured speaker:

```
Speaker ID 13 — 青山龍星 ノーマル (Aoyama Ryusei Normal) — formal male voice
```

Voice parameters tuned for natural phone calls:

| Parameter | Value | Effect |
|-----------|-------|--------|
| speedScale | 0.85 | 15% slower than default |
| pitchScale | 0.0 | Neutral pitch |
| intonationScale | 0.8 | Slightly reduced intonation |
| volumeScale | 1.0 | Default volume |

#### 200ms leading silence

All Piper-generated audio starts with 200ms of silence. This is required because the GSM module's PCM channel needs a brief moment to stabilize after being opened — without this, the first syllable gets clipped.

#### Signaling beep

The system can append a soft beep to signal the attendant it's their turn to speak (like an answering machine beep):

- **Frequency:** 880 Hz
- **Duration:** 150ms
- **Amplitude:** 10% (soft)
- **Envelope:** 20ms fade-in and fade-out (prevents clicks)
- **Gap:** 80ms of silence before the beep

---

### 6.4 call_context.py — Per-call language memory

**File:** `/app/core/call_context.py`

Solves a specific problem: the system could potentially handle multiple simultaneous calls, each with a different language. A Python `ContextVar` ensures each async task has its own independent language variable — a call in Japanese won't contaminate a call in Portuguese.

#### Functions

| Function | What it does |
|----------|-------------|
| `get_lang()` | Returns the current call's language (`"pt"` or `"ja"`) |
| `set_call_lang(lang)` | Sets the language and returns a reset token |
| `reset_call_lang(token)` | Restores the previous state using the token |

The token/reset pattern ensures the language is correctly reset even if an error occurs during the call (via `finally` block).

---

## 7. Telegram interface — bot/handlers.py

**File:** `/app/bot/handlers.py`

Controls all Telegram interaction: receives messages, interprets intent, and coordinates other modules.

### How a message is processed

**1. Language extraction** (`extract_lang`)

| Message tag | Call language |
|-------------|--------------|
| `[ja]` or `[jp]` or 🇯🇵 | Japanese |
| `[pt]` or `[br]` or 🇧🇷 | Portuguese (default) |
| No tag | Portuguese (default) |

The tag is stripped from the message before further processing.

**2. Phone number extraction** (`extract_phone`)

Looks for digit sequences that look like a phone number (minimum 10 characters, accepts `-`, `+`, `(`, `)`).

**3. Business hours check** (`check_business_hours`)

Checks if it's business hours in Japan (9am–5pm, Mon–Fri, JST). Sends a warning if not — but does not block the call.

**4. Translation and preview**

If the message has a phone number:
- Translates context to Japanese (if needed)
- Sends a voice preview to Telegram for confirmation
- Starts the call in the background

If no phone number:
- Translates and sends a voice preview only

### Main call loop (`execute_call`)

```
Phase 1 — Setup
  ├── Set language in ContextVar
  ├── Generate and pre-synthesize opening message
  └── Dial the number

Phase 2 — Wait for answer
  └── Wait up to 30 seconds for "VOICE CALL: BEGIN"

Phase 3 — Conversation loop (max 10 turns)
  │
  ├── Play agent audio (with beep at end)
  ├── Record attendant speech (VAD, max 8 seconds)
  ├── Send audio to Telegram (for diagnostics)
  ├── Transcribe with Whisper (STT)
  ├── Evaluate confidence:
  │     ├── < 20%: silence/noise → ask attendant to repeat (max 2x)
  │     ├── 20-55%: uncertain → Claude reasons about what was said
  │     └── > 55%: normal → process directly
  ├── Send to Claude → get response
  ├── Check response flags:
  │     ├── [TRANSFERINDO]: notify Telegram, keep listening
  │     ├── [AGUARDANDO]: notify Telegram, keep listening
  │     └── [ENCERRAR]: play farewell, exit loop
  └── Synthesize response with TTS

Phase 4 — Closing
  ├── Stop audio playback and recording
  ├── Disable PCM on GSM module
  ├── Send AT+CHUP (hang up)
  ├── Generate call summary via Claude
  └── Save to database and send summary to Telegram
```

### Per-turn timing sent to Telegram

```
⏱ Recording: 2.3s | STT: 3.1s | LLM: 0.8s | TTS: 0.3s | total: 4.2s
```

### Silence detection

If STT detects no speech for 2 consecutive turns, the system ends the call automatically.

---

## 8. Telephony — telephony/

### 8.1 call_manager.py — GSM module communication

**File:** `/app/telephony/call_manager.py`

The bridge between software and GSM hardware. Sends AT commands and interprets responses.

#### What are AT commands?

AT commands are text instructions sent via serial port to modems and GSM modules. Created in the 1980s by Hayes, they are the universal modem control standard. Examples:

| Command | Function |
|---------|----------|
| `AT` | Communication test ("are you there?") |
| `ATD052XXXXXXXX;` | Dial a number (`;` indicates voice call) |
| `ATA` | Answer an incoming call |
| `AT+CHUP` | Hang up |
| `AT+CSQ` | Query signal strength (0-31) |
| `AT+CEREG?` | Query network registration status |
| `AT+CPCMFRM=1` | Set PCM format: 16kHz, 16 bits |
| `AT+CPCMREG=1` | Enable PCM audio channel |
| `AT+CECM=1` | Enable external mic/speaker via PCM |

> **Why AT+CHUP and not ATH?** ATH is the standard hang-up command, but on this specific module (SIM7600G-H), ATH does not end voice calls. AT+CHUP ("Call Hang-Up") works correctly.

#### Audio initialization sequence

When a call is answered, the system activates the PCM audio channel in sequence:

```
AT+CPCMFRM=1   → set format: 16kHz mono 16 bits
AT+CPCMREG=1   → register PCM channel (opens /dev/ttyUSB4)
AT+CECM=1      → connect microphone and speaker to PCM channel
```

#### Unsolicited events

The GSM module sends spontaneous messages on the same serial channel. `call_manager` has a **background thread** continuously reading these:

| Event received | Meaning |
|---------------|---------|
| `RING` | Incoming call |
| `+CLIP: "052XXXXXXXX"...` | Caller ID |
| `VOICE CALL: BEGIN` | Call answered and active |
| `VOICE CALL: END` | Call ended |
| `NO CARRIER` | Call failed or ended remotely |

#### Concurrent access protection

The serial port can only process one command at a time. `call_manager` uses a **mutex** to ensure two commands are never sent simultaneously:

```
Command 1 ───► Lock ───► Send ───► Wait for response ───► Unlock
                                                               │
Command 2 ── Waits in queue ───────────────────────────────► Lock ► ...
```

---

### 8.2 audio.py — Audio recording and playback

**File:** `/app/telephony/audio.py`

Manages real-time audio during a call: sends agent voice to the GSM module and receives attendant voice.

#### What is PCM audio?

PCM (Pulse-Code Modulation) is the most basic digital audio format — raw sample values in sequence, no compression. With no codec or decoding needed, latency is minimal.

The GSM module configured with `AT+CPCMFRM=1` operates at:
- **Sample rate:** 16,000 Hz
- **Bit depth:** 16 bits per sample
- **Channels:** 1 (mono)

#### VAD — Voice Activity Detection

VAD distinguishes speech from silence/noise. The system uses **WebRTC VAD** (the same used by Google Chrome for calls).

VAD operates on **30ms frames** and classifies each as "speech" or "silence". Recording logic:

```
Start: wait for speech
  ├── First 800ms ignored (discards TTS echo from just-played audio)
  ├── Requires 5 consecutive speech frames (150ms) to confirm start
  │
During recording:
  ├── Keep recording while there is speech
  ├── Detect silence after 300ms continuous silence
  │     └── If at least 300ms of speech was recorded: end
  └── Safety timeout: maximum 8 seconds
```

**Aggressiveness = 1** means the VAD is moderate — not too sensitive (won't confuse noise with speech) and not too conservative (won't cut real speech).

#### Extended tolerance for first frames

For the first 5 frames after opening the serial port, the system uses a 500ms timeout instead of the normal 90ms. The serial port needs a few milliseconds to stabilize after being opened.

#### Audio playback

Reads the WAV file and sends data through the GSM serial port in 20ms blocks. Per block:

1. Read WAV data
2. Convert to 16kHz mono if necessary
3. Apply noise gate (suppress noise below 1% of peak)
4. Send via serial port
5. Wait until the next 20ms interval (clock sync)

Clock synchronization is important: sending data too fast or too slow causes distorted (sped-up or slowed-down) voice on the attendant's end.

---

## 9. Database — utils/db.py

**File:** `/app/utils/db.py`

Uses SQLite, stored in a single file: `/data/historico.db`. No external database server required.

### Tables

#### `ligacoes_saida` — Outbound call history

| Field | Type | Description |
|-------|------|-------------|
| id | Integer | Unique ID |
| data_hora | Text | Date and time |
| numero | Text | Dialed number |
| contexto_pt | Text | Your instruction in Portuguese |
| script_jp | Text | Agent's opening message |
| status | Text | "concluida", "erro", etc. |
| resultado | Text | AI-generated summary |

#### `ligacoes_recebidas` — Inbound call history

| Field | Type | Description |
|-------|------|-------------|
| id | Integer | Unique ID |
| data_hora | Text | Date and time |
| numero_origem | Text | Caller's number |
| transcricao_jp | Text | What the caller said |
| resumo_pt | Text | Summary in Portuguese |
| status | Text | Attendance status |

#### `transcricoes` — Individual conversation turns

| Field | Type | Description |
|-------|------|-------------|
| ligacao_id | Integer | Reference to call |
| turno | Integer | Turn number (1, 2, 3...) |
| papel | Text | "Atendente" or "Agente" |
| texto_jp | Text | What was said |

#### `resumos` — Full call summaries

| Field | Type | Description |
|-------|------|-------------|
| ligacao_id | Integer | Reference to call |
| duracao_turnos | Integer | How many turns the call lasted |
| resumo_pt | Text | AI-generated summary in Portuguese |
| transcricao_completa | Text | Full formatted conversation |

---

## 10. Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token (via @BotFather) |
| `TELEGRAM_CHAT_ID` | Yes | Your numeric Telegram ID (security — only accepts your messages) |
| `LANG_MODE` | No | Default language: `pt` (default) or `ja` |

---

## 11. Full call flow

Example: you send to Telegram:

```
"[ja] 052-1234-5678 I want to schedule a medical appointment"
```

**Step 1 — Telegram receives the message**  
`handle_text()` in `handlers.py` receives the message.

**Step 2 — Language extraction**  
`extract_lang()` detects `[ja]` → language = Japanese, clean text = `"052-1234-5678 I want to schedule a medical appointment"`

**Step 3 — Phone number extraction**  
`extract_phone()` detects `052-1234-5678`, context = `"I want to schedule a medical appointment"`

**Step 4 — Business hours check**  
`check_business_hours()` checks JST and sends a warning if outside business hours.

**Step 5 — Translation to Japanese**  
`translate_to_japanese(...)` → Claude Haiku translates to Japanese: `"診察の予約をしたいのですが。"`

**Step 6 — Voice preview**  
The system synthesizes the Japanese text with VOICEVOX and sends the audio to Telegram for confirmation before calling.

**Step 7 — Call initialization (`execute_call`)**  
- Sets language `ja` in ContextVar  
- Generates opening: `"はい、私はお客様の代理としてご連絡しております。診察の予約をしたいのですが。"`  
- Synthesizes with VOICEVOX + adds beep

**Step 8 — Dialing**  
`call_manager.dial("052-1234-5678")` → sends `ATD052-1234-5678;` via serial. Waits up to 30 seconds for `VOICE CALL: BEGIN`.

**Step 9 — Audio activation**  
`call_manager.enable_pcm_audio()` → sends `AT+CPCMFRM=1`, `AT+CPCMREG=1`, `AT+CECM=1`.

**Step 10 — Turn 1: Agent speaks**  
`audio_manager.play_and_wait()` → sends opening WAV via serial port (attendant hears opening + beep).

**Step 11 — Turn 1: Agent listens**  
`audio_manager.record_turn()` → VAD waits for speech. Attendant says something (e.g., "Tanaka Clinic, good afternoon!").

**Step 12 — Transcription**  
`speech_to_text()` → Whisper transcribes: `"田中クリニックです、こんにちは"` with 87% confidence. WAV sent to Telegram for diagnostics.

**Step 13 — AI processes**  
`process_call_turn("田中クリニックです、こんにちは")` → Claude Haiku responds: `"診察の予約をお願いしたいのですが、よろしいでしょうか。"` (no special tags)

**Step 14 — Agent responds**  
`text_to_speech("診察の予約をお願いしたいのですが、よろしいでしょうか。", beep=True)` → VOICEVOX synthesizes + adds beep.

**Step 15 — Loop continues**  
Up to 10 turns. When the attendant says "ありがとうございました", Claude detects the farewell and responds with `[終了]失礼しました。`. The system:
1. Plays the farewell without a beep
2. Exits the loop immediately

**Step 16 — Closing**  
`call_manager.disable_pcm_audio()` → `AT+CPCMREG=0,1`  
`call_manager.hangup()` → `AT+CHUP`

**Step 17 — Summary**  
`generate_call_summary(...)` → Claude generates in Portuguese:

```
Goal: Schedule a medical appointment at Tanaka Clinic.
Result: Appointment scheduled for Apr 25 at 2pm.
Next steps: Bring health insurance card.
```

Summary sent to Telegram. Everything saved to the database.

---

## 12. Language system

### How to specify language

By default, all calls are made in Portuguese. For Japanese, add a tag at the start of the message:

| Format | Language |
|--------|----------|
| `[ja] 052-XXXX-XXXX context` | Japanese |
| `[jp] 052-XXXX-XXXX context` | Japanese |
| `🇯🇵 052-XXXX-XXXX context` | Japanese |
| `052-XXXX-XXXX context` | Portuguese (default) |

### What changes between languages

| Component | PT mode | JA mode |
|-----------|---------|---------|
| TTS | Piper local (pt_BR-faber-medium) | VOICEVOX (青山龍星) |
| STT | Whisper with Portuguese prompt | Whisper with Japanese prompt |
| AI | Portuguese system prompt | Formal Japanese system prompt |
| Opening | "Olá, meu nome é..." | "はい、私は...の代理として..." |
| Event tags | `[TRANSFERINDO]`, `[AGUARDANDO]`, `[ENCERRAR]` | `[転送]`, `[保留]`, `[終了]` |
| Repeat prompt | "Poderia repetir, por favor?" | "恐れ入りますが、もう一度おっしゃっていただけますでしょうか。" |

### Per-call isolation

The language is stored in a Python `ContextVar`. Each call has its own independent language — if the system ever supports multiple simultaneous calls, each will have its language isolated without interference.

---

## 13. Error handling and edge cases

### Low STT confidence

| Confidence | Action |
|-----------|--------|
| < 20% | Silence or pure noise — ask attendant to repeat |
| 20% to 55% | Uncertain — Claude reasons about what was said and asks for confirmation |
| > 55% | Normal — process directly |

### Two consecutive silences

If STT detects no speech for 2 consecutive turns, the system assumes the call was remotely ended (or there is an audio problem) and terminates automatically.

### Farewell detection

When Claude detects the attendant has said goodbye, it responds with `[ENCERRAR]` prefix. The system then:
1. Plays the farewell **without a beep**
2. Exits the loop immediately
3. Does not wait for further attendant speech

### Call transfer

When the attendant says they will transfer to another extension, Claude prefixes `[TRANSFERINDO]`. The system notifies Telegram but **does not end the call** — it keeps listening for the next person.

### Hold

When the attendant asks to hold, Claude prefixes `[AGUARDANDO]`. The system notifies Telegram and keeps waiting.

### LLM timeout

If the Claude API takes more than 15 seconds, the system does not stall. It catches the error, notifies Telegram, and sends a courtesy response ("Sorry, one moment please.") to the attendant.

### Dial timeout

If nobody answers within 30 seconds, the system sends `AT+CHUP` to cancel the call and notifies Telegram with an error message.

---

## 14. Telegram commands

| Command | Function |
|---------|----------|
| `/status` | Full system status (TTS, STT, GSM, database, time, queue, language) |
| `/perfil` | Shows registered profile data |
| `/historico` | Last 5 outbound calls with status and result |
| `/recados` | Last 5 inbound calls with summary |
| `/fila` | Pending calls in the retry queue |
| `/resumo` | Summary of the currently active call |
| `/retentar` | Execute next call in the queue |
| `/desligar` | Force-end the active call via AT+CHUP |
| `/limpar` | Clear all call history and the queue |
| `/transcricao` | Show last 3 full transcripts with summaries |
| `/help` | Show this command list with usage examples |

### Usage examples

```
# Call in Portuguese
052-1234-5678 schedule a medical appointment

# Call in Japanese
[ja] 052-1234-5678 check exam results

# Check system before calling
/status

# See last call
/historico

# If the call got stuck
/desligar
```

---

## 15. Known limits

### Per-turn latency

Time between attendant finishing speech and agent responding: approximately **4 to 5 seconds**:

| Step | Approximate time |
|------|-----------------|
| VAD (detect end of speech) | 300ms |
| STT — Whisper small | ~3,000ms |
| LLM — Claude Haiku | ~800ms |
| TTS — Piper or VOICEVOX | ~200ms |
| **Total** | **~4,300ms** |

The main bottleneck is Whisper, which by design always processes a 30-second audio window regardless of actual recording length. This cannot be worked around with the `small` model.

### Maximum turns per call

- **Outbound calls:** 10 turns
- **Inbound calls:** 8 turns

After reaching the limit, the system ends the call automatically.

### Simultaneous calls

The system supports **only 1 active call at a time**. The physical GSM module has a single SIM chip.

### Half-duplex audio

The system alternates between speaking and listening — it is not possible to interrupt the agent while it speaks. Each turn follows:
1. Agent speaks
2. Attendant speaks
3. Agent speaks
4. ...

### Power dependency

If the server goes down during a call, the call will remain active on the GSM chip until the carrier's timeout (usually 3–5 minutes). There is no automatic call recovery.

---

## 16. Glossary

| Term | Definition |
|------|-----------|
| **AT Commands** | Text commands for controlling modems and GSM modules. "AT" stands for "Attention". |
| **asyncio** | Python library for running multiple tasks "at the same time" (async concurrency) without multiple threads. |
| **beam_size** | Whisper parameter controlling how many transcription hypotheses are evaluated in parallel. beam_size=1 is fastest (always picks the highest-probability option). |
| **ContextVar** | A Python variable whose value is independent per async task. Enables safe "global variables" in concurrent code. |
| **half-duplex** | Communication in only one direction at a time (alternating). Opposite of full-duplex (simultaneous). |
| **int8** | 8-bit numeric format. Used to compress AI models — reduces memory and increases speed with minimal quality loss. |
| **JST** | Japan Standard Time — UTC+9. |
| **Keigo** | Formal/respectful Japanese. Includes honorific and humble speech forms. |
| **mutex** | A locking mechanism that ensures only one process/thread accesses a resource at a time. |
| **PCM** | Pulse-Code Modulation — uncompressed digital audio format, used for real-time audio. |
| **resample** | Convert audio from one sample rate to another (e.g., 22050 Hz → 16000 Hz). |
| **STT** | Speech-to-Text — transcription of speech to text. |
| **TTS** | Text-to-Speech — voice synthesis from text. |
| **token (ContextVar)** | Identifier returned by `ContextVar.set()`, used to restore the previous value via `reset()`. |
| **VAD** | Voice Activity Detection — algorithm that detects when there is active speech in audio. |
| **VHQ** | Very High Quality — soxr's maximum quality mode for audio resampling. |

---

*Documentation — agente-ligacao v1.0*
