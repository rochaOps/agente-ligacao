# Agente Telefônico com IA — Documentação Técnica

**Versão:** 1.0  
**Data:** Abril de 2026  
**Autor:** Luis Rocha  
**Plataforma:** Self-hosted Linux server / SIM7600G-H

---

## Índice

1. [O que é este sistema](#1-o-que-é-este-sistema)
2. [Como o sistema funciona — visão geral](#2-como-o-sistema-funciona--visão-geral)
3. [Infraestrutura e hardware](#3-infraestrutura-e-hardware)
4. [Estrutura de arquivos](#4-estrutura-de-arquivos)
5. [Módulo principal — main.py](#5-módulo-principal--mainpy)
6. [Núcleo de inteligência — core/](#6-núcleo-de-inteligência--core)
   - 6.1 [agent.py — O cérebro da ligação](#61-agentpy--o-cérebro-da-ligação)
   - 6.2 [stt.py — Ouvidos do agente](#62-sttpy--ouvidos-do-agente)
   - 6.3 [tts.py — Voz do agente](#63-ttspy--voz-do-agente)
   - 6.4 [call_context.py — Memória de idioma por ligação](#64-call_contextpy--memória-de-idioma-por-ligação)
7. [Interface Telegram — bot/handlers.py](#7-interface-telegram--bothandlerspy)
8. [Telefonia — telephony/](#8-telefonia--telephony)
   - 8.1 [call_manager.py — Comunicação com o módulo GSM](#81-call_managerpy--comunicação-com-o-módulo-gsm)
   - 8.2 [audio.py — Gravação e reprodução de áudio](#82-audiopy--gravação-e-reprodução-de-áudio)
9. [Banco de dados — utils/db.py](#9-banco-de-dados--utilsdbpy)
10. [Variáveis de ambiente](#10-variáveis-de-ambiente)
11. [Fluxo completo de uma ligação](#11-fluxo-completo-de-uma-ligação)
12. [Sistema de idiomas](#12-sistema-de-idiomas)
13. [Tratamento de erros e casos especiais](#13-tratamento-de-erros-e-casos-especiais)
14. [Comandos do Telegram](#14-comandos-do-telegram)
15. [Limites e restrições conhecidas](#15-limites-e-restrições-conhecidas)
16. [Glossário](#16-glossário)

---

## 1. O que é este sistema

Este é um **agente telefônico automatizado com inteligência artificial**. Ele permite que o usuário, via Telegram, ordene ao sistema que ligue para um número de telefone e conduza uma conversa em português ou japonês de forma completamente autônoma.

### O que o sistema faz

- Recebe instruções pelo Telegram (ex.: "ligue para 052-XXXX-XXXX e agende uma consulta médica")
- Disca o número usando um chip GSM físico instalado no servidor
- Fala com o atendente usando uma voz sintetizada (Text-to-Speech)
- Ouve a resposta do atendente e transcreve (Speech-to-Text)
- Envia a transcrição para a IA (Claude) que decide o que responder
- Continua a conversa até o objetivo ser cumprido ou a ligação encerrar
- Envia um resumo da ligação de volta ao Telegram

### O que o sistema **não** faz

- Não usa VoIP nem internet para fazer ligações — usa chip físico real
- Não depende de serviços externos de TTS/STT — tudo roda localmente
- Não liga sem autorização do usuário — toda ligação é iniciada manualmente pelo Telegram

---

## 2. Como o sistema funciona — visão geral

Imagine um funcionário que:
1. Recebe uma tarefa no Telegram
2. Liga para o número com seu telefone físico
3. Fala com o atendente usando um roteiro
4. Ouve o que o atendente diz
5. Pensa na resposta certa
6. Responde e continua a conversa
7. Desliga e te manda um resumo

Este sistema faz exatamente isso, de forma digital. Cada etapa tem um componente responsável:

```
VOCÊ (Telegram)
      │
      ▼
[bot/handlers.py]  ← recebe suas mensagens e comanda tudo
      │
      ├──► [telephony/call_manager.py]  ← faz a ligação via chip GSM
      │
      ├──► [core/tts.py]  ← transforma texto em voz (fala)
      │
      ├──► [telephony/audio.py]  ← envia/recebe áudio pelo chip
      │
      ├──► [core/stt.py]  ← transforma o que ouviu em texto
      │
      └──► [core/agent.py]  ← decide o que responder (IA Claude)
```

---

## 3. Infraestrutura e hardware

O sistema roda em um servidor Linux self-hosted com Docker.

| Componente | Descrição |
|-----------|-----------|
| Módulo GSM | SIM7600G-H (conectado via USB) |
| Runtime | Docker + Python 3.12 |
| API de IA | Anthropic Claude API |

---|---|
| Processador | AMD Threadripper 1950X (16 núcleos / 32 threads) |
| Memória RAM | 32 GB |
| Armazenamento | SSD |
| Módulo GSM | SIM7600G-H (HAT conectado diretamente ao servidor) |

### Portas seriais do módulo GSM

O módulo SIM7600G-H aparece no sistema operacional como dois "arquivos de dispositivo":

- `/dev/ttyUSB2` — canal de **comandos AT** (instruções de controle: discar, desligar, verificar sinal)
- `/dev/ttyUSB4` — canal de **áudio PCM** (dados de voz em tempo real durante a ligação)

> **Por que duas portas?** O módulo GSM separa controle e dados. É como ter um controle remoto (USB2) e um cabo de áudio (USB4) sendo dois cabos distintos para o mesmo aparelho.

### Serviços de software

| Serviço | Onde roda | Para que serve |
|---|---|---|
| FastAPI | No próprio servidor | Servidor principal do agente |
| VOICEVOX | Container Docker local | Síntese de voz em japonês |
| SQLite | Arquivo local `/data/historico.db` | Banco de dados de ligações |
| Piper TTS | Modelo local em memória | Síntese de voz em português |
| faster-whisper | Modelo local em memória | Transcrição de fala |

---

## 4. Estrutura de arquivos

```
/app/
├── main.py                    # Ponto de entrada — inicia tudo
├── core/
│   ├── agent.py               # IA (Claude) — raciocínio e respostas
│   ├── stt.py                 # Speech-to-Text — ouvir e transcrever
│   ├── tts.py                 # Text-to-Speech — gerar voz
│   └── call_context.py        # Controle de idioma por ligação
├── bot/
│   └── handlers.py            # Interface Telegram — comandos e lógica de ligação
├── telephony/
│   ├── call_manager.py        # Controle do chip GSM via AT commands
│   ├── audio.py               # Gravação e reprodução de áudio PCM
│   └── adb_handler.py         # Utilitário de status GSM
├── utils/
│   └── db.py                  # Banco de dados SQLite
└── log_config.json            # Configuração de logs

/config/
└── user_profile.json          # Seus dados pessoais (nome, endereço, plano de saúde, etc.)

/data/
└── historico.db               # Banco de dados SQLite (criado automaticamente)
```

---

## 5. Módulo principal — main.py

**Arquivo:** `/app/main.py`

Este é o **ponto de entrada** do sistema. Quando o servidor é iniciado (via Docker ou diretamente), é este arquivo que é executado primeiro.

### O que acontece na inicialização

Quando o sistema liga, ele executa uma sequência de preparação na seguinte ordem:

```
1. Inicializa o banco de dados (cria as tabelas se não existirem)
2. Carrega o modelo Whisper (STT) na memória — ~10 segundos
3. Carrega o modelo Piper (TTS português) na memória — ~3 segundos
4. Inicializa o módulo GSM (SIM7600G-H) — verifica sinal e registro
5. Inicia o bot do Telegram (começa a ouvir mensagens)
```

> **Por que pré-carregar os modelos?** Modelos de IA são arquivos grandes. Carregá-los durante uma ligação causaria um atraso de 10+ segundos. Ao carregar na inicialização, ficam na RAM prontos para uso imediato.

### Rotas de diagnóstico (debug)

O sistema expõe alguns endpoints HTTP para testes:

| Rota | Função |
|---|---|
| `GET /health` | Verifica se o servidor está respondendo |
| `GET /debug/hangup` | Força o desligamento da ligação atual |
| `GET /debug/dial/{numero}` | Disca um número para teste |
| `GET /debug/at/{comando}` | Envia um comando AT diretamente ao chip GSM |
| `GET /test/call/{numero}` | Executa uma ligação de teste completa com áudio |

---

## 6. Núcleo de inteligência — core/

### 6.1 agent.py — O cérebro da ligação

**Arquivo:** `/app/core/agent.py`

Este módulo é responsável por toda a comunicação com a IA (Claude da Anthropic) e por manter o histórico da conversa durante uma ligação.

#### Perfil do usuário

Na inicialização, o sistema lê o arquivo `/config/user_profile.json` que contém seus dados pessoais:

```
Nome, data de nascimento, telefone, endereço, plano de saúde, empresa, cônjuge, etc.
```

Esses dados são usados pela IA quando precisar informar algo ao atendente (ex.: quando perguntarem o nome ou CPF). **Esses dados nunca são listados ao atendente** — a IA os usa apenas quando necessário para avançar a conversa.

#### Prompts do sistema (instruções para a IA)

O sistema cria dois conjuntos de instruções para a IA, um para cada idioma:

**Modo português** — instrui a IA a:
- Responder sempre em português brasileiro
- Usar no máximo 2 frases curtas por resposta
- Ser direto como em uma ligação telefônica real
- Nunca revelar ou listar os dados cadastrados
- Detectar quando o atendente vai transferir (`[TRANSFERINDO]`)
- Detectar quando o atendente pediu para aguardar (`[AGUARDANDO]`)
- Detectar despedidas e responder com `[ENCERRAR]`

**Modo japonês** — instrui a IA a:
- Usar japonês formal (keigo — linguagem respeitosa)
- Responder em 1 a 2 frases curtas
- Usar os dados do perfil quando necessário
- Detectar eventos de transferência (`[転送]`), espera (`[保留]`) e encerramento (`[終了]`)

#### Histórico da conversa

Durante cada ligação, o sistema mantém um histórico dos últimos **20 turnos** da conversa. Esse histórico é enviado para a IA a cada resposta, permitindo que ela entenda o contexto do que foi dito anteriormente.

> **Analogia:** É como se a IA tivesse um caderninho onde anota as últimas 20 falas da conversa. Antes de responder, ela relê o caderninho para entender o que está acontecendo.

#### Tratamento de transcrição incerta

Quando o STT transcreve o áudio com baixa confiança (entre 20% e 55%), o sistema não tenta responder diretamente. Em vez disso, envia a transcrição incerta para a IA com uma instrução especial:

```
"Tente deduzir o que foi dito com base no contexto desta ligação.
 Formule uma pergunta de confirmação natural em 1 frase curta."
```

A IA então raciocina sobre o que provavelmente foi dito e pergunta ao atendente para confirmar. Por exemplo, se o STT captou "cps", a IA pode perguntar "Você perguntou sobre o meu CPF?".

#### Parâmetros da API Claude

| Parâmetro | Valor | Motivo |
|---|---|---|
| Modelo | `claude-haiku-4-5-20251001` | Mais rápido e barato para respostas curtas |
| Max tokens | 150 | Respostas curtas de telefone (1-2 frases) |
| Timeout | 15 segundos | Evita travar a ligação se a API estiver lenta |
| Histórico | Últimos 20 turnos | Janela de contexto suficiente sem custo excessivo |

---

### 6.2 stt.py — Ouvidos do agente

**Arquivo:** `/app/core/stt.py`

Este módulo transforma o áudio gravado durante a ligação em texto (Speech-to-Text). Usa o modelo **faster-whisper** da OpenAI, rodando localmente na CPU.

#### Modelo utilizado

```
Whisper "small" — int8 — 8 threads de CPU
```

**Por que "small" e não um modelo menor?** Os modelos `tiny` e `base` do Whisper cometem erros graves em português. Em testes, "Meu nome é Luis Rocha" virou "Eu não me aluiço, roxa". O modelo `small` tem qualidade aceitável.

**Por que int8?** É uma técnica de compressão que reduz o uso de memória e acelera o processamento, com perda mínima de qualidade. Em vez de armazenar cada número com 32 bits, usa 8 bits.

**Por que 8 threads?** Usando 8 threads para o Whisper, o tempo de transcrição caiu significativamente em relação ao padrão.

#### Limitação conhecida do Whisper

O Whisper sempre processa internamente uma janela de **30 segundos** de áudio, mesmo que o áudio gravado seja de 2 segundos. Isso é uma característica da arquitetura do modelo e **não pode ser contornada sem trocar de modelo**. Por isso, o STT sempre leva cerca de 3 segundos, independente do tamanho do áudio.

#### Normalização de áudio

Antes de transcrever, o sistema verifica se o áudio está no formato correto:
- **Taxa de amostragem:** 16.000 Hz (16kHz)
- **Canais:** Mono (1 canal)

Se o áudio chegou em formato diferente (ex.: estéreo a 8kHz), o sistema converte automaticamente usando a biblioteca `soxr` com qualidade máxima (VHQ).

#### Resultado retornado

Após a transcrição, o sistema retorna:

```python
{
    "texto":           "o que foi falado pelo atendente",
    "confianca":       0.85,      # de 0.0 a 1.0 (85% de confiança)
    "pedir_repeticao": False      # True se confiança < 50%
}
```

---

### 6.3 tts.py — Voz do agente

**Arquivo:** `/app/core/tts.py`

Este módulo transforma texto em áudio de voz (Text-to-Speech). Usa motores diferentes dependendo do idioma da ligação.

#### Motor para português — Piper TTS

**Piper** é um sistema TTS neural de código aberto que roda completamente offline. O modelo utilizado é:

```
pt_BR-faber-medium.onnx  — voz masculina brasileira, qualidade média
```

- Latência: ~200ms para frases curtas
- Sem dependência de internet
- Gera áudio a 22.050 Hz, que é resampleado para 16.000 Hz para o pipeline

#### Motor para japonês — VOICEVOX

**VOICEVOX** é um sistema TTS japonês rodando em container Docker local. O falante configurado é:

```
Speaker ID 13 — 青山龍星 ノーマル (Aoyama Ryusei Normal) — voz masculina formal
```

Parâmetros de voz ajustados para soar natural em ligações telefônicas:

| Parâmetro | Valor | Efeito |
|---|---|---|
| speedScale | 0.85 | 15% mais lento que o padrão |
| pitchScale | 0.0 | Tom neutro |
| intonationScale | 0.8 | Entonação ligeiramente reduzida |
| volumeScale | 1.0 | Volume padrão |

#### Silêncio inicial de 200ms

Todo áudio gerado pelo Piper começa com 200ms de silêncio. Isso é necessário porque o canal PCM do módulo GSM precisa de um breve momento para "estabilizar" após ser aberto. Sem esse silêncio, a primeira sílaba da fala seria cortada.

#### Bip de sinalização

O sistema pode adicionar um bip suave ao final do áudio para sinalizar ao atendente que é a vez dele falar (como o bip de secretária eletrônica):

- **Frequência:** 880 Hz
- **Duração:** 150ms
- **Amplitude:** 10% (suave, não assustador)
- **Envelope:** fade-in e fade-out de 20ms (evita cliques)
- **Gap:** 80ms de silêncio antes do bip

---

### 6.4 call_context.py — Memória de idioma por ligação

**Arquivo:** `/app/core/call_context.py`

Este módulo resolve um problema específico: o sistema pode potencialmente lidar com múltiplas ligações simultâneas (em teoria), e cada ligação pode ter um idioma diferente. Como garantir que a ligação em japonês não "contamine" a ligação em português?

A solução é uma **variável de contexto** (`ContextVar`) do Python. Funciona como uma variável global, mas cada tarefa assíncrona tem sua própria cópia independente.

> **Analogia:** Imagine que cada ligação é um funcionário diferente. Cada funcionário tem seu próprio post-it com "idioma desta ligação". Mesmo que dois funcionários estejam trabalhando ao mesmo tempo, cada um lê seu próprio post-it.

#### Funções

| Função | O que faz |
|---|---|
| `get_lang()` | Retorna o idioma da ligação atual (`"pt"` ou `"ja"`) |
| `set_call_lang(lang)` | Define o idioma e retorna um "token" para desfazer depois |
| `reset_call_lang(token)` | Restaura o estado anterior usando o token |

O padrão token/reset garante que, mesmo se ocorrer um erro durante a ligação, o idioma seja resetado corretamente (usando bloco `finally`).

---

## 7. Interface Telegram — bot/handlers.py

**Arquivo:** `/app/bot/handlers.py`

Este é o módulo que controla toda a interação via Telegram. Ele recebe suas mensagens, interpreta o que você quer fazer, e coordena os outros módulos para executar.

### Como uma mensagem é processada

Quando você envia uma mensagem de texto ao bot:

**1. Extração de idioma** (`extract_lang`)

O sistema verifica se há uma tag de idioma na mensagem:

| Tag na mensagem | Idioma da ligação |
|---|---|
| `[ja]` ou `[jp]` ou 🇯🇵 | Japonês |
| `[pt]` ou `[br]` ou 🇧🇷 | Português (padrão) |
| Sem tag | Português (padrão) |

A tag é removida da mensagem antes de continuar o processamento.

**2. Extração de número** (`extract_phone`)

O sistema procura por sequências de dígitos que pareçam um número de telefone (mínimo 10 caracteres, aceita `-`, `+`, `(`, `)`).

**3. Verificação de horário comercial** (`check_business_hours`)

Verifica se são horário comercial no Japão (9h-17h, segunda a sexta, timezone JST). Se não for, envia um aviso — mas não bloqueia a ligação.

**4. Tradução e prévia**

Se a mensagem tem número de telefone:
- Traduz o contexto para japonês (se necessário)
- Envia uma prévia de voz ao Telegram para você conferir
- Inicia a ligação em segundo plano

Se não tem número:
- Apenas traduz e envia a prévia de voz

### Loop principal de ligação (`execute_call`)

Esta é a função mais importante do sistema. Ela conduz toda a ligação do início ao fim:

```
Fase 1 — Preparação
  ├── Define idioma no contexto (ContextVar)
  ├── Gera e pré-sintetiza a mensagem de abertura
  └── Disca o número

Fase 2 — Aguardar atender
  └── Espera até 30 segundos pelo evento "VOICE CALL: BEGIN"

Fase 3 — Loop de conversa (máximo 10 turnos)
  │
  ├── Toca o áudio do agente (com bip ao final)
  ├── Grava o que o atendente diz (VAD, máx. 8 segundos)
  ├── Envia o áudio ao Telegram (para diagnóstico)
  ├── Transcreve com Whisper (STT)
  ├── Avalia a confiança:
  │     ├── < 20%: silêncio/ruído → pede repetição (máx. 2x)
  │     ├── 20-55%: incerto → LLM raciocinará sobre o que foi dito
  │     └── > 55%: normal → processa diretamente
  ├── Envia para Claude (LLM) → obtém resposta
  ├── Verifica flags na resposta:
  │     ├── [TRANSFERINDO]: notifica Telegram, continua ouvindo
  │     ├── [AGUARDANDO]: notifica Telegram, continua ouvindo
  │     └── [ENCERRAR]: toca despedida, sai do loop
  └── Sintetiza resposta com TTS

Fase 4 — Encerramento
  ├── Para reprodução e gravação de áudio
  ├── Desabilita PCM no módulo GSM
  ├── Envia AT+CHUP (desliga a chamada)
  ├── Gera resumo da ligação via Claude
  └── Salva no banco de dados e envia resumo ao Telegram
```

### Métricas de tempo enviadas ao Telegram

A cada turno, o sistema envia ao Telegram o tempo gasto em cada etapa:

```
⏱ Gravação: 2.3s | STT: 3.1s | LLM: 0.8s | TTS: 0.3s | total: 4.2s
```

Isso permite identificar gargalos sem precisar acessar os logs do servidor.

### Detecção de silêncio

Se o STT não detectar fala por 2 turnos consecutivos, o sistema encerra a ligação automaticamente para não ficar travado.

---

## 8. Telefonia — telephony/

### 8.1 call_manager.py — Comunicação com o módulo GSM

**Arquivo:** `/app/telephony/call_manager.py`

Este módulo é a "ponte" entre o software e o hardware GSM. Ele envia comandos AT ao módulo e interpreta as respostas.

#### O que são comandos AT?

Comandos AT são instruções textuais enviadas via porta serial para modems e módulos GSM. Criados nos anos 1980 pela Hayes, são o padrão universal para controle de modems. Exemplos:

| Comando | Função |
|---|---|
| `AT` | Teste de comunicação ("você está aí?") |
| `ATD052XXXXXXXX;` | Disca o número (o `;` indica chamada de voz) |
| `ATA` | Atende uma chamada recebida |
| `AT+CHUP` | Desliga a chamada |
| `AT+CSQ` | Consulta intensidade do sinal (0-31) |
| `AT+CEREG?` | Consulta status de registro na rede |
| `AT+CPCMFRM=1` | Define formato PCM: 16kHz, 16 bits |
| `AT+CPCMREG=1` | Habilita canal de áudio PCM |
| `AT+CECM=1` | Habilita microfone/speaker externos via PCM |

> **Por que AT+CHUP e não ATH?** O comando ATH é o padrão para desligar, mas neste módulo específico (SIM7600G-H), o ATH não desliga a chamada de voz. O AT+CHUP ("Call Hang-Up") funciona corretamente.

#### Sequência de inicialização do áudio

Quando a ligação é atendida, o sistema precisa ativar o canal de áudio PCM em sequência:

```
AT+CPCMFRM=1   → define formato: 16kHz mono 16 bits
AT+CPCMREG=1   → registra o canal PCM (abre /dev/ttyUSB4)
AT+CECM=1      → conecta microfone e speaker ao canal PCM
```

#### Eventos não solicitados (unsolicited events)

O módulo GSM envia mensagens espontâneas pelo mesmo canal serial, sem que o software tenha pedido. O `call_manager` tem um **thread em segundo plano** que fica lendo continuamente essas mensagens:

| Evento recebido | Significado |
|---|---|
| `RING` | Está chegando uma ligação |
| `+CLIP: "052XXXXXXXX"...` | Número de quem está ligando |
| `VOICE CALL: BEGIN` | A ligação foi atendida e está ativa |
| `VOICE CALL: END` | A ligação foi encerrada |
| `NO CARRIER` | Chamada falhou ou foi encerrada remotamente |

#### Proteção contra acesso simultâneo

O módulo serial só consegue processar um comando por vez. O `call_manager` usa um **mutex** (trava) para garantir que dois comandos nunca sejam enviados ao mesmo tempo:

```
Comando 1 ───► Trava ───► Envia ───► Aguarda resposta ───► Libera
                                                                │
Comando 2 ── Espera na fila ──────────────────────────────────► Trava ► ...
```

---

### 8.2 audio.py — Gravação e reprodução de áudio

**Arquivo:** `/app/telephony/audio.py`

Este módulo gerencia o fluxo de áudio em tempo real durante a ligação: envia voz do agente ao módulo GSM e recebe a voz do atendente.

#### O que é áudio PCM?

PCM (Pulse-Code Modulation) é o formato mais básico de áudio digital — simplesmente os valores das amostras de som gravadas em sequência, sem compressão. Como não há codec ou decodificação necessária, a latência é mínima.

O módulo GSM configurado com `AT+CPCMFRM=1` opera com:
- **Taxa de amostragem:** 16.000 Hz (16.000 amostras por segundo)
- **Profundidade de bits:** 16 bits por amostra
- **Canais:** 1 (mono)

#### VAD — Voice Activity Detection

VAD é um algoritmo que distingue fala de silêncio/ruído. O sistema usa o **WebRTC VAD** (o mesmo usado pelo Google Chrome para chamadas).

O VAD opera em frames de **30ms** e classifica cada frame como "fala" ou "silêncio". A lógica de gravação é:

```
Início: aguarda fala aparecer
  ├── Primeiros 800ms ignorados (descarta eco do TTS que acabou de tocar)
  ├── Requer 5 frames consecutivos de fala (150ms) para confirmar início
  │
Durante a gravação:
  ├── Continua gravando enquanto há fala
  ├── Detecta silêncio após 300ms contínuos
  │     └── Se já gravou pelo menos 300ms de fala: encerra
  └── Timeout de segurança: máximo 8 segundos
```

**Aggressiveness = 1** significa que o VAD é moderado — não é muito sensível (não confunde ruído com fala) nem muito conservador (não corta fala real).

#### Primeiros frames com tolerância estendida

Nos primeiros 5 frames após abrir a porta serial, o sistema usa um timeout de 500ms em vez dos 90ms normais. Isso é necessário porque a porta serial precisa de alguns milissegundos para estabilizar após ser aberta, e durante esse tempo pode não enviar dados a tempo.

#### Reprodução de áudio

O processo de reprodução lê o arquivo WAV e envia os dados pela porta serial do módulo GSM em blocos de 20ms. Para cada bloco:

1. Lê os dados do arquivo WAV
2. Converte para 16kHz mono se necessário
3. Aplica noise gate (suprime ruídos muito baixos abaixo de 1% do pico)
4. Envia pela porta serial
5. Aguarda até o próximo intervalo de 20ms (sincronização de clock)

A sincronização de clock é importante: se o sistema enviar dados muito rápido ou muito devagar, a voz soará distorcida (acelerada ou desacelerada) no telefone do atendente.

---

## 9. Banco de dados — utils/db.py

**Arquivo:** `/app/utils/db.py`

O sistema usa SQLite, um banco de dados que fica armazenado em um único arquivo: `/data/historico.db`. Não requer servidor de banco de dados externo.

### Tabelas

#### `ligacoes_saida` — Histórico de ligações realizadas

| Campo | Tipo | Descrição |
|---|---|---|
| id | Inteiro | Identificador único |
| data_hora | Texto | Data e hora da ligação |
| numero | Texto | Número discado |
| contexto_pt | Texto | Instrução em português que você deu |
| script_jp | Texto | O que o agente disse na abertura |
| status | Texto | "concluida", "erro", etc. |
| resultado | Texto | Resumo gerado pela IA |

#### `ligacoes_recebidas` — Histórico de ligações recebidas

| Campo | Tipo | Descrição |
|---|---|---|
| id | Inteiro | Identificador único |
| data_hora | Texto | Data e hora |
| numero_origem | Texto | Número de quem ligou |
| transcricao_jp | Texto | O que o chamador disse |
| resumo_pt | Texto | Resumo em português |
| status | Texto | Status do atendimento |

#### `transcricoes` — Turnos individuais de conversa

Armazena cada fala individualmente para análise posterior.

| Campo | Tipo | Descrição |
|---|---|---|
| ligacao_id | Inteiro | Referência à ligação |
| turno | Inteiro | Número do turno (1, 2, 3...) |
| papel | Texto | "Atendente" ou "Agente" |
| texto_jp | Texto | O que foi dito |

#### `resumos` — Resumos completos de ligações

| Campo | Tipo | Descrição |
|---|---|---|
| ligacao_id | Inteiro | Referência à ligação |
| duracao_turnos | Inteiro | Quantos turnos durou |
| resumo_pt | Texto | Resumo em português pela IA |
| transcricao_completa | Texto | Toda a conversa formatada |

---

## 10. Variáveis de ambiente

Para funcionar, o sistema precisa das seguintes variáveis configuradas:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sim | Chave da API da Anthropic para usar o Claude |
| `TELEGRAM_BOT_TOKEN` | Sim | Token do bot do Telegram (obtido via @BotFather) |
| `TELEGRAM_CHAT_ID` | Sim | Seu ID numérico no Telegram (para segurança — só aceita mensagens suas) |
| `LANG_MODE` | Não | Idioma padrão: `pt` (padrão) ou `ja` |

---

## 11. Fluxo completo de uma ligação

Este é o fluxo detalhado do que acontece quando você envia ao Telegram:

```
"[ja] 052-1234-5678 quero agendar uma consulta médica"
```

### Passo 1 — Telegram recebe a mensagem

`handle_text()` em `handlers.py` recebe a mensagem.

### Passo 2 — Extração de idioma

`extract_lang()` detecta `[ja]` → idioma = japonês, texto limpo = `"052-1234-5678 quero agendar uma consulta médica"`

### Passo 3 — Extração do número

`extract_phone()` detecta `052-1234-5678`, contexto = `"quero agendar uma consulta médica"`

### Passo 4 — Verificação de horário

`check_business_hours()` verifica horário JST e envia aviso se fora do horário comercial.

### Passo 5 — Tradução para japonês

`translate_to_japanese("quero agendar uma consulta médica", lang="ja")` → Claude Haiku traduz para japonês: `"診察の予約をしたいのですが。"`

### Passo 6 — Prévia de voz

O sistema sintetiza o texto japonês com VOICEVOX e envia o áudio ao Telegram para você conferir antes de ligar.

### Passo 7 — Inicialização da ligação (`execute_call`)

- Define idioma `ja` na ContextVar
- Gera mensagem de abertura: `"はい、私はLuis Rochaの代理としてご連絡しております。診察の予約をしたいのですが。"`
- Sintetiza com VOICEVOX + adiciona bip

### Passo 8 — Discagem

`call_manager.dial("052-1234-5678")` → envia `ATD052-1234-5678;` pela porta serial. Aguarda até 30 segundos pelo evento `VOICE CALL: BEGIN`.

### Passo 9 — Ativação do áudio

`call_manager.enable_pcm_audio()` → envia `AT+CPCMFRM=1`, `AT+CPCMREG=1`, `AT+CECM=1`.

### Passo 10 — Turno 1: Agente fala

`audio_manager.play_and_wait()` → envia o WAV com a abertura pela porta serial (o atendente ouve a mensagem de abertura + bip).

### Passo 11 — Turno 1: Agente ouve

`audio_manager.record_turn()` → VAD aguarda fala. O atendente fala algo (ex.: "Clínica Tanaka, boa tarde!").

### Passo 12 — Transcrição

`speech_to_text()` → Whisper transcreve: `"田中クリニックです、こんにちは"` com confiança de 87%.

Arquivo WAV é enviado ao Telegram para diagnóstico.

### Passo 13 — IA processa

`process_call_turn("田中クリニックです、こんにちは")` → Claude Haiku recebe o histórico + a fala do atendente e responde: `"診察の予約をお願いしたいのですが、よろしいでしょうか。"` (sem tags especiais)

### Passo 14 — Agente responde

`text_to_speech("診察の予約をお願いしたいのですが、よろしいでしょうか。", beep=True)` → VOICEVOX sintetiza + adiciona bip.

### Passo 15 — Continua o loop

O loop continua por até 10 turnos. Se o atendente disser "ありがとうございました" (obrigado, encerrando), Claude detecta o encerramento e responde com `[終了]失礼しました。`. O sistema:
1. Toca a despedida sem bip
2. Sai do loop imediatamente

### Passo 16 — Encerramento

- `call_manager.disable_pcm_audio()` → `AT+CPCMREG=0,1`
- `call_manager.hangup()` → `AT+CHUP`

### Passo 17 — Resumo

`generate_call_summary("quero agendar uma consulta médica")` → Claude gera em português:

```
Objetivo: Agendar consulta médica na Clínica Tanaka.
Resultado: Consulta agendada para 25/04 às 14h.
Próximos passos: Levar cartão do plano de saúde.
```

Resumo enviado ao Telegram. Tudo salvo no banco de dados.

---

## 12. Sistema de idiomas

### Como especificar idioma

Por padrão, todas as ligações são feitas em português. Para japonês, adicione uma tag no início da mensagem:

| Forma | Idioma |
|---|---|
| `[ja] 052-XXXX-XXXX contexto` | Japonês |
| `[jp] 052-XXXX-XXXX contexto` | Japonês |
| `🇯🇵 052-XXXX-XXXX contexto` | Japonês |
| `052-XXXX-XXXX contexto` | Português (padrão) |

### O que muda entre os idiomas

| Componente | Modo PT | Modo JA |
|---|---|---|
| TTS | Piper local (pt_BR-faber-medium) | VOICEVOX (青山龍星) |
| STT | Whisper com prompt em português | Whisper com prompt em japonês |
| IA | System prompt em português | System prompt em japonês formal |
| Abertura | "Olá, meu nome é Assistente Virtual..." | "はい、私は{nome}の代理として..." |
| Tags de evento | `[TRANSFERINDO]`, `[AGUARDANDO]`, `[ENCERRAR]` | `[転送]`, `[保留]`, `[終了]` |
| Prompt de repetição | "Poderia repetir, por favor?" | "恐れ入りますが、もう一度おっしゃっていただけますでしょうか。" |

### Isolamento por ligação

O idioma é armazenado em uma ContextVar do Python. Isso significa que cada ligação tem seu próprio idioma independente. Se no futuro o sistema suportar múltiplas ligações simultâneas, cada uma terá seu idioma isolado sem interferência.

---

## 13. Tratamento de erros e casos especiais

### Confiança baixa do STT

| Confiança | Ação |
|---|---|
| < 20% | Silêncio ou ruído puro — pede ao atendente que repita |
| 20% a 55% | Incerto — Claude raciocinará sobre o texto e pedirá confirmação |
| > 55% | Normal — processa diretamente |

### Dois silêncios consecutivos

Se o STT não detectar fala em 2 turnos seguidos, o sistema assume que a ligação foi encerrada remotamente (ou que há problema de áudio) e encerra automaticamente.

### Detecção de despedida

Quando Claude detecta que o atendente se despediu (palavras como "tchau", "até logo", "obrigado" com tom de encerramento), ele responde com prefixo `[ENCERRAR]`. O sistema então:
1. Toca a despedida **sem bip** (não faz sentido sinalizar turno após despedida)
2. Sai do loop imediatamente
3. Não espera pela próxima fala do atendente

### Transferência de ligação

Quando o atendente diz que vai transferir para outro ramal, Claude prefixo `[TRANSFERINDO]`. O sistema notifica o Telegram mas **não encerra** — continua ouvindo a próxima pessoa.

### Espera (hold)

Quando o atendente pede para aguardar, Claude prefixo `[AGUARDANDO]`. O sistema notifica o Telegram e continua aguardando, preparando a próxima resposta.

### Timeout de LLM

Se a API do Claude demorar mais de 15 segundos (ex.: congestionamento de rede), o sistema não trava. Ele captura o erro, notifica o Telegram e envia uma resposta de cortesia ("Desculpe, um momento por favor.") para o atendente.

### Timeout de discagem

Se ninguém atender em 30 segundos, o sistema envia `AT+CHUP` para cancelar a chamada e notifica o Telegram com a mensagem de erro.

---

## 14. Comandos do Telegram

| Comando | Função |
|---|---|
| `/status` | Status completo do sistema (TTS, STT, GSM, banco, horário, fila, idioma) |
| `/perfil` | Exibe seus dados cadastrados (nome, endereço, plano de saúde) |
| `/historico` | Últimas 5 ligações realizadas com status e resultado |
| `/recados` | Últimas 5 ligações recebidas com resumo |
| `/fila` | Ligações pendentes na fila de retentativa |
| `/resumo` | Resumo da ligação que está em andamento agora |
| `/retentar` | Executa a próxima ligação da fila |
| `/desligar` | Força o encerramento da ligação ativa via AT+CHUP |
| `/limpar` | Apaga todo o histórico do banco de dados e limpa a fila |
| `/transcricao` | Exibe as últimas 3 transcrições completas com resumo |
| `/help` | Exibe esta lista de comandos com exemplos de uso |

### Exemplos de uso via Telegram

```
# Ligar em português
052-1234-5678 agendar consulta médica

# Ligar em japonês
[ja] 052-1234-5678 consultar resultado de exame

# Verificar o sistema antes de ligar
/status

# Ver última ligação
/historico

# Se a ligação travou
/desligar
```

---

## 15. Limites e restrições conhecidas

### Latência total por turno

O tempo entre o atendente terminar de falar e o agente responder é de aproximadamente **4 a 5 segundos**, distribuídos assim:

| Etapa | Tempo aproximado |
|---|---|
| VAD (detectar fim de fala) | 300ms |
| STT — Whisper small (transcrição) | ~3.000ms |
| LLM — Claude Haiku (resposta) | ~800ms |
| TTS — Piper ou VOICEVOX (síntese) | ~200ms |
| **Total** | **~4.300ms** |

O gargalo principal é o Whisper, que por design interno processa sempre uma janela de 30 segundos de áudio, independente do tamanho real da gravação. Isso não é contornável com o modelo `small`.

### Máximo de turnos por ligação

- **Ligações realizadas:** 10 turnos
- **Ligações recebidas:** 8 turnos

Após atingir o limite, o sistema encerra a ligação automaticamente.

### Ligações simultâneas

O sistema suporta **apenas 1 ligação ativa por vez**. O módulo GSM físico tem apenas 1 chip.

### Áudio em uma direção por vez (half-duplex)

O sistema alterna entre falar e ouvir — não é possível interromper o agente enquanto ele fala (como em uma chamada telefônica normal). A cada turno:
1. Agente fala
2. Atendente fala
3. Agente fala
4. ...

### Dependência de energia

Se o servidor cair durante uma ligação, a chamada ficará ativa no chip GSM até o timeout do operador (geralmente 3-5 minutos). Não há recuperação automática de ligação.

---

## 16. Glossário

| Termo | Definição |
|---|---|
| **AT Commands** | Comandos de texto para controlar modems e módulos GSM. "AT" vem de "Attention". |
| **asyncio** | Biblioteca do Python para executar múltiplas tarefas "ao mesmo tempo" (concorrência assíncrona) sem usar múltiplos threads. |
| **beam_size** | Parâmetro do Whisper que controla quantas hipóteses de transcrição ele avalia em paralelo. beam_size=1 é o mais rápido (escolhe sempre a opção com maior probabilidade). |
| **ContextVar** | Variável do Python cujo valor é independente para cada tarefa assíncrona em execução. Permite "variáveis globais" seguras em código concorrente. |
| **half-duplex** | Comunicação em apenas uma direção por vez (alternado). Oposto de full-duplex (simultâneo). |
| **int8** | Formato numérico de 8 bits. Usado para comprimir modelos de IA — reduz memória e aumenta velocidade com perda mínima de qualidade. |
| **JST** | Japan Standard Time — UTC+9. Fuso horário do Japão. |
| **Keigo** | Japonês formal/respeitoso. Inclui formas honoríficas e humildes de falar. |
| **mutex** | Mecanismo de trava que garante que apenas um processo/thread acesse um recurso por vez. |
| **PCM** | Pulse-Code Modulation — formato de áudio digital sem compressão, usado para áudio em tempo real. |
| **resample** | Converter áudio de uma taxa de amostragem para outra (ex.: 22050 Hz → 16000 Hz). |
| **STT** | Speech-to-Text — transcrição de fala para texto. |
| **TTS** | Text-to-Speech — síntese de voz a partir de texto. |
| **token (ContextVar)** | Identificador retornado por `set()` de uma ContextVar, usado para restaurar o valor anterior via `reset()`. |
| **VAD** | Voice Activity Detection — algoritmo que detecta quando há fala ativa no áudio. |
| **VHQ** | Very High Quality — modo de qualidade máxima do soxr para resample de áudio. |

---

*Documentação gerada em Abril de 2026. Sistema em operação no homeserver de Luis Rocha.*
