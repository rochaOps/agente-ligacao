import os

VERSION = "0.2.0"

# ── Serial / modem ────────────────────────────────────────────────────────────
AT_PORT    = os.getenv("AT_PORT",    "/dev/ttyGSM_at")
AUDIO_PORT = os.getenv("AUDIO_PORT", "/dev/ttyGSM_pcm")
BAUD_RATE  = 115200

# ── Audio ─────────────────────────────────────────────────────────────────────
SAMPLE_RATE  = 16000
SAMPLE_WIDTH = 2       # 16-bit PCM
CHANNELS     = 1

# ── Paths ─────────────────────────────────────────────────────────────────────
TMP_DIR      = "/tmp"
DB_PATH      = "/data/agente.db"
PROFILE_PATH = "/config/user_profile.json"

# ── Whisper / STT ─────────────────────────────────────────────────────────────
WHISPER_MODEL        = os.getenv("WHISPER_MODEL",        "small")
WHISPER_DEVICE       = os.getenv("WHISPER_DEVICE",       "cpu")
WHISPER_THREADS      = int(os.getenv("WHISPER_THREADS",  "4"))
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
CONFIDENCE_MIN       = float(os.getenv("CONFIDENCE_MIN",       "0.4"))
CONFIDENCE_UNCERTAIN = float(os.getenv("CONFIDENCE_UNCERTAIN", "0.6"))

# ── VOICEVOX / TTS ────────────────────────────────────────────────────────────
VOICEVOX_URL        = os.getenv("VOICEVOX_URL",        "http://voicevox:50021")
VOICEVOX_SPEAKER_ID = int(os.getenv("VOICEVOX_SPEAKER_ID", "13"))  # 青山龍星 ノーマル — voz masculina formal

# ── LLM / Claude ──────────────────────────────────────────────────────────────
MODEL_AGENT           = os.getenv("MODEL_AGENT",           "claude-sonnet-4-6")
MODEL_FAST            = os.getenv("MODEL_FAST",            "claude-haiku-4-5-20251001")
LLM_TIMEOUT           = int(os.getenv("LLM_TIMEOUT",           "30"))
LLM_MAX_TOKENS_AGENT  = int(os.getenv("LLM_MAX_TOKENS_AGENT",  "1024"))
LLM_MAX_TOKENS_FAST   = int(os.getenv("LLM_MAX_TOKENS_FAST",   "256"))

# ── Call behaviour ────────────────────────────────────────────────────────────
MAX_CALL_TURNS        = int(os.getenv("MAX_CALL_TURNS",        "10"))
MAX_INCOMING_TURNS    = int(os.getenv("MAX_INCOMING_TURNS",    "8"))
MAX_RECORD_DURATION   = int(os.getenv("MAX_RECORD_DURATION",   "8"))
CALL_ANSWER_TIMEOUT   = int(os.getenv("CALL_ANSWER_TIMEOUT",   "30"))
SILENCE_STREAK_MAX    = int(os.getenv("SILENCE_STREAK_MAX",    "2"))

# ── Localisation ──────────────────────────────────────────────────────────────
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")

# ── Environment validation ────────────────────────────────────────────────────
_REQUIRED_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ANTHROPIC_API_KEY",
)


def validate_env() -> None:
    missing = [var for var in _REQUIRED_ENV if not os.getenv(var)]
    if missing:
        raise RuntimeError(
            f"Variáveis de ambiente obrigatórias ausentes: {', '.join(missing)}"
        )
