import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

from config import DB_PATH


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager: opens a connection, commits on success, rolls back on error."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    with _db() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS ligacoes_saida (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                numero TEXT NOT NULL,
                contexto_pt TEXT,
                script_jp TEXT,
                status TEXT DEFAULT 'pendente',
                resultado TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS ligacoes_recebidas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                numero_origem TEXT,
                transcricao_jp TEXT,
                resumo_pt TEXT,
                status TEXT DEFAULT 'nao_retornado'
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS transcricoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ligacao_id INTEGER NOT NULL,
                numero TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                turno INTEGER NOT NULL,
                papel TEXT NOT NULL CHECK(papel IN ('agente', 'atendente')),
                texto_jp TEXT,
                texto_pt TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS resumos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ligacao_id INTEGER NOT NULL,
                numero TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                duracao_turnos INTEGER,
                resumo_pt TEXT,
                transcricao_completa TEXT
            )
        ''')


def salvar_ligacao_saida(numero: str, contexto_pt: str, script_jp: str,
                         status: str = "pendente", resultado: Optional[str] = None) -> int:
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO ligacoes_saida (data_hora, numero, contexto_pt, script_jp, status, resultado) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), numero, contexto_pt, script_jp, status, resultado)
        )
        return c.lastrowid


def salvar_turno_transcricao(ligacao_id: int, numero: str, turno: int,
                              papel: str, texto_jp: str, texto_pt: Optional[str] = None) -> None:
    with _db() as conn:
        conn.cursor().execute(
            "INSERT INTO transcricoes (ligacao_id, numero, data_hora, turno, papel, texto_jp, texto_pt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ligacao_id, numero, _now(), turno, papel, texto_jp, texto_pt)
        )


def salvar_resumo(ligacao_id: int, numero: str, resumo_pt: str,
                  transcricao_completa: str, duracao_turnos: int) -> None:
    with _db() as conn:
        conn.cursor().execute(
            "INSERT INTO resumos (ligacao_id, numero, data_hora, duracao_turnos, resumo_pt, transcricao_completa) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ligacao_id, numero, _now(), duracao_turnos, resumo_pt, transcricao_completa)
        )


def buscar_transcricao(ligacao_id: int) -> list:
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT turno, papel, texto_jp, texto_pt, data_hora FROM transcricoes "
            "WHERE ligacao_id = ? ORDER BY turno ASC",
            (ligacao_id,)
        )
        return c.fetchall()


def buscar_resumos(limite: int = 5) -> list:
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT ligacao_id, numero, data_hora, duracao_turnos, resumo_pt "
            "FROM resumos ORDER BY id DESC LIMIT ?",
            (limite,)
        )
        return c.fetchall()


def buscar_historico(limite: int = 10) -> list:
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT data_hora, numero, contexto_pt, status, resultado "
            "FROM ligacoes_saida ORDER BY id DESC LIMIT ?",
            (limite,)
        )
        return c.fetchall()


def buscar_recados(limite: int = 10) -> list:
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT data_hora, numero_origem, resumo_pt, status "
            "FROM ligacoes_recebidas ORDER BY id DESC LIMIT ?",
            (limite,)
        )
        return c.fetchall()


def salvar_ligacao_recebida(numero_origem: str, transcricao_jp: str, resumo_pt: str) -> None:
    with _db() as conn:
        conn.cursor().execute(
            "INSERT INTO ligacoes_recebidas (data_hora, numero_origem, transcricao_jp, resumo_pt) "
            "VALUES (?, ?, ?, ?)",
            (_now(), numero_origem, transcricao_jp, resumo_pt)
        )


def atualizar_status_saida(ligacao_id: int, status: str, resultado: Optional[str] = None) -> None:
    with _db() as conn:
        conn.cursor().execute(
            "UPDATE ligacoes_saida SET status = ?, resultado = ? WHERE id = ?",
            (status, resultado, ligacao_id)
        )
