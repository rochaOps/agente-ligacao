import json
import logging
import os
from typing import Any

import anthropic

from config import (
    MODEL_AGENT, MODEL_FAST,
    LLM_TIMEOUT, LLM_MAX_TOKENS_AGENT, LLM_MAX_TOKENS_FAST,
    PROFILE_PATH,
)

logger = logging.getLogger(__name__)


def load_profile() -> dict[str, Any]:
    if not os.path.exists(PROFILE_PATH):
        logger.warning(f"Perfil não encontrado em {PROFILE_PATH} — usando perfil vazio")
        return {}
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Perfil JSON inválido em {PROFILE_PATH}: {e}")
        return {}


profile = load_profile()


def get_user_name() -> str:
    return profile.get("nome_japones", profile.get("nome_romaji", "Luis Rocha"))


_endereco  = profile.get("endereco", {})
_seguro    = profile.get("seguro_saude", {})
_trabalho  = profile.get("trabalho", {})
_familia   = profile.get("familia", {})
_mensagens = "、".join(profile.get("mensagens_disponiveis", []))

SYSTEM_PROMPT = f"""あなたは{profile.get('nome_japones', '')}（{profile.get('nome_romaji', '')}）さんの代理として日本語で電話をかけるAIエージェントです。

=== 本人情報 ===
氏名: {profile.get('nome_japones', '')}（{profile.get('nome_romaji', '')}）
国籍: {profile.get('nacionalidade', '')}
生年月日: {profile.get('data_nascimento', '')}
在留カード番号: {profile.get('zairyu_number', '')}
在留資格: {profile.get('zairyu_tipo', '')}
在留期限: {profile.get('zairyu_validade', '')}
携帯電話: {profile.get('telefone_celular', '')}
メッセージ連絡: {_mensagens}でも連絡可能

=== 住所 ===
〒{_endereco.get('cep', '')} {_endereco.get('prefeitura', '')} {_endereco.get('cidade', '')} {_endereco.get('bairro', '')} {_endereco.get('numero', '')}

=== 健康保険 ===
保険者名称: {_seguro.get('seguradora', '')}
保険者番号: {_seguro.get('numero_seguradora', '')}
被保険者番号: {_seguro.get('numero_segurado', '')}
有効期限: {_seguro.get('validade', '')}

=== 勤務先 ===
{_trabalho.get('empresa_japones', '')} / {_trabalho.get('cidade', '')}

=== 家族 ===
配偶者: {_familia.get('esposa_nome_japones', '')} / {_familia.get('esposa_telefone', '')}

=== 最重要ルール ===
1. 常に敬語（丁寧語・謙譲語）を使用すること
2. 返答は必ず1〜2文以内の短い文にすること
3. 日本語のみを返答すること（説明・注釈・ポルトガル語不可）
4. 質問や確認は一切しないこと
5. 電話での自然な会話として簡潔に表現すること
6. 本人情報が必要な場合は上記の情報を使用すること
7. 日本語が話せないことを伝える場合: 「{profile.get('contexto_padrao', '')}」

=== 転送・保留・終了の検出 ===
相手の発言に以下が含まれる場合、返答の先頭にタグを付けること:
- [転送]: 「お繋ぎします」「転送します」「担当者に」「お回しします」
- [保留]: 「お待ちください」「少々お待ちください」「お調べします」「確認いたします」「少し待ってください」
- [終了]: 「ありがとうございました」「失礼します」「さようなら」「よろしくお願いします（締め）」「またご連絡ください」「お電話ありがとうございました」「以上でよろしいでしょうか」など会話の終わりを示す発言

=== あなたの立場（必ず守ること）===
あなたは常にこちらから電話をかけている側（発信者）です。

【確認の「はい」と用件完了の区別】
相手が「はい」「わかりました」「承知しました」と言っても、それだけでは用件完了ではありません。
以下を必ず確認すること:

- 導入・自己紹介への返答（「代わりにご連絡しております」「〇〇の件で…」への「はい」）
  → 用件はまだ始まっていない。続けて本題を伝える。
- 本題の途中での返答（情報確認・予約・質問の回答待ちなど）
  → 用件はまだ完了していない。次のステップを続ける。
- 用件の目的（予約完了・情報取得・確認など）が実際に達成された後の返答
  → 用件完了。丁寧に締めくくる（「ありがとうございました。失礼いたします。」）

【禁止事項】
- 「お待ちしております」は使わない — 待つのは相手であり、あなたではない
- 導入への「はい」だけで通話を終了しない

=== 予期しない質問への対応 ===
- 書類について聞かれた場合: 「必要な書類をご確認いただけますでしょうか。」
- 担当者の名前を聞かれた場合: 「{profile.get('nome_japones', '')}の代理でご連絡しております。」
- 折り返しを提案された場合: 「{profile.get('telefone_celular', '')}にご連絡いただけますでしょうか。」
- 理解できなかった場合: 「恐れ入りますが、もう一度おっしゃっていただけますでしょうか。」
"""

_anthropic_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_call_history: list[dict[str, str]] = []
_call_turn: int = 0


def start_call_session() -> None:
    global _call_history, _call_turn
    _call_history = []
    _call_turn    = 0


def get_call_turn() -> int:
    return _call_turn


def get_call_summary() -> str:
    if not _call_history:
        return "Nenhuma conversa registrada."
    lines = []
    for i, entry in enumerate(_call_history):
        papel = "Atendente" if entry["role"] == "user" else "Agente"
        lines.append(f"[{i+1}] {papel}: {entry['content']}")
    return "\n".join(lines)


def get_full_transcript() -> str:
    return get_call_summary()


async def translate_to_portuguese(text_jp: str) -> str:
    """Translate Japanese → Portuguese (used for Telegram log only)."""
    message = await _anthropic_client.messages.create(
        model=MODEL_FAST,
        max_tokens=LLM_MAX_TOKENS_FAST,
        system="Traduza o texto a seguir do japonês para o português. Retorne apenas a tradução, sem explicações.",
        messages=[{"role": "user", "content": text_jp}],
    )
    return message.content[0].text.strip()


async def translate_to_japanese(text_pt: str) -> str:
    """Translate a Portuguese instruction into natural telephone Japanese."""
    message = await _anthropic_client.messages.create(
        model=MODEL_FAST,
        max_tokens=LLM_MAX_TOKENS_FAST,
        system=(
            "Traduza o texto a seguir para japonês formal e natural, adequado para ser falado "
            "por quem está FAZENDO uma ligação telefônica comercial. "
            "Retorne apenas a tradução em japonês, sem explicações nem comentários."
        ),
        messages=[{"role": "user", "content": text_pt}],
    )
    return message.content[0].text.strip()


async def evaluate_context(phone: str, context_pt: str) -> dict[str, Any]:
    """
    Avalia se o contexto fornecido é suficiente para o agente conduzir a ligação.
    Retorna: {"sufficient": bool, "question": str | None}
    """
    message = await _anthropic_client.messages.create(
        model=MODEL_FAST,
        max_tokens=200,
        system=(
            "Você avalia se há contexto suficiente para um agente de IA fazer uma ligação telefônica no Japão. "
            "O agente precisa de informações suficientes para responder perguntas típicas do atendente "
            "(ex: nome, motivo detalhado, datas, referências específicas). "
            "Responda APENAS em JSON válido: "
            '{\"sufficient\": true/false, \"question\": \"pergunta em português ou null\"}'
            "\n\nSe sufficient=true, question deve ser null. "
            "Se sufficient=false, question deve ser UMA pergunta curta e objetiva sobre o que falta. "
            "Contexto vazio ou genérico demais (ex: só 'ligar', 'perguntar') = insufficient."
        ),
        messages=[{
            "role": "user",
            "content": f"Número: {phone}\nContexto: {context_pt}"
        }],
    )
    import json as _json
    try:
        return _json.loads(message.content[0].text.strip())
    except Exception:
        return {"sufficient": True, "question": None}


async def process_call_turn(
    transcribed_text: str,
    uncertain: bool = False,
    in_hold: bool = False,
    turn: int = 0,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    global _call_history, _call_turn

    _call_turn += 1

    if uncertain:
        clean_user_content = (
            f"[STT低信頼度 — テキスト]: \"{transcribed_text}\"\n\n"
            "文脈から意味を最大限推測し、自然に返答してください。"
            "本当に理解できない場合のみ「恐れ入りますが、もう一度おっしゃっていただけますでしょうか。」と返してください。"
        )
    else:
        clean_user_content = transcribed_text

    if in_hold:
        clean_user_content = f"[保留明け] {clean_user_content}"

    elapsed_min = int(elapsed_seconds // 60)
    elapsed_sec = int(elapsed_seconds % 60)
    context_prefix = f"[通話情報: ターン{turn}、経過時間{elapsed_min}分{elapsed_sec}秒] "
    api_user_content = context_prefix + clean_user_content

    _call_history.append({"role": "user", "content": clean_user_content})
    if len(_call_history) > 20:
        _call_history = _call_history[-20:]

    messages_for_api = _call_history[:-1] + [{"role": "user", "content": api_user_content}]

    message = await _anthropic_client.messages.create(
        model=MODEL_AGENT,
        max_tokens=LLM_MAX_TOKENS_AGENT,
        system=SYSTEM_PROMPT,
        messages=messages_for_api,
        timeout=LLM_TIMEOUT,
    )

    response_text = message.content[0].text.strip()
    _call_history.append({"role": "assistant", "content": response_text})

    is_transfer = "[転送]" in response_text
    is_hold     = "[保留]" in response_text
    is_end      = "[終了]" in response_text
    clean_response = (
        response_text
        .replace("[転送]", "").replace("[保留]", "").replace("[終了]", "")
        .strip()
    )

    return {
        "resposta_jp": clean_response,
        "is_transfer": is_transfer,
        "is_hold":     is_hold,
        "is_end":      is_end,
        "turno":       _call_turn,
    }


async def generate_call_summary(contexto_pt: str) -> str:
    if not _call_history:
        return "Ligação sem conversa registrada."

    message = await _anthropic_client.messages.create(
        model=MODEL_FAST,
        max_tokens=300,
        system=(
            "Você é um assistente que resume conversas telefônicas em japonês para o usuário em português. "
            "Seja conciso e objetivo. Inclua: objetivo da ligação, o que foi acordado, próximos passos se houver."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Objetivo da ligação: {contexto_pt}\n\n"
                f"Transcrição:\n{get_full_transcript()}\n\n"
                "Faça um resumo em português."
            ),
        }],
    )
    return message.content[0].text.strip()


async def process_incoming_call(transcribed_text: str) -> str:
    incoming_prompt = SYSTEM_PROMPT + """
=== 着信モード ===
今は電話を受けている状況です。
丁寧に対応し、ご用件とお名前をお聞きすること。
必要に応じて折り返し連絡の旨を伝えること。
"""
    message = await _anthropic_client.messages.create(
        model=MODEL_FAST,
        max_tokens=LLM_MAX_TOKENS_FAST,
        system=incoming_prompt,
        messages=[{"role": "user", "content": f"相手からの発言: {transcribed_text}"}],
    )
    return message.content[0].text.strip()
