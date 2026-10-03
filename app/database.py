from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "clinical_engine.db"
DEFAULT_SEED_PATH = ROOT / "data" / "hierarchical_bank.json"

def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path
    return Path(os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))


def get_db_connection(db_path: Path | None = None) -> sqlite3.Connection:
    target = _resolve_db_path(db_path)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Initialize SQLite database with questions_bank and patient_sessions tables."""
    target = _resolve_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection(target) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS questions_bank (
                id TEXT PRIMARY KEY,
                system TEXT NOT NULL,
                level TEXT NOT NULL,
                text TEXT NOT NULL,
                rationale TEXT,
                jev_primitive TEXT NOT NULL,
                trigger_condition TEXT
            );
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS patient_sessions (
                session_id TEXT NOT NULL,
                question_asked_id TEXT,
                patient_response TEXT,
                jev_confidence_score REAL NOT NULL,
                timestamp DATETIME NOT NULL
            );
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_questions_system_level 
            ON questions_bank (system, level);
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sessions_session_id 
            ON patient_sessions (session_id);
            """
        )
        conn.commit()
    LOGGER.info("Initialized SQLite database at %s", db_path)


def seed_database(seed_path: Path = DEFAULT_SEED_PATH, db_path: Path | None = None) -> int:
    """Seed questions_bank from JSON if file exists using INSERT OR IGNORE."""
    if not seed_path.exists():
        LOGGER.warning("Seed file not found at %s. Skipping database seed.", seed_path)
        return 0

    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON list in seed file {seed_path}")

    inserted_count = 0
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        for item in payload:
            cursor.execute(
                """
                INSERT OR IGNORE INTO questions_bank (
                    id, system, level, text, rationale, jev_primitive, trigger_condition
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item["id"]),
                    str(item["system"]),
                    str(item["level"]),
                    str(item["text"]),
                    item.get("rationale", ""),
                    str(item.get("jev_primitive", "Choice")),
                    item.get("trigger_condition", ""),
                ),
            )
            if cursor.rowcount > 0:
                inserted_count += 1
        conn.commit()

    LOGGER.info(
        "Seeded questions_bank from %s: %d questions loaded/updated",
        seed_path,
        inserted_count,
    )
    return inserted_count


def log_patient_session(
    session_id: str,
    question_asked_id: str | None,
    patient_response: str | None,
    jev_confidence_score: float,
    db_path: Path | None = None,
) -> None:
    """Audit log a routed decision and Jev confidence score into patient_sessions."""
    utc_now = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO patient_sessions (
                session_id, question_asked_id, patient_response, jev_confidence_score, timestamp
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                question_asked_id,
                patient_response or "",
                float(jev_confidence_score),
                utc_now,
            ),
        )
        conn.commit()


def get_session_history(session_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Retrieve audit history for a patient session."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, question_asked_id, patient_response, jev_confidence_score, timestamp
            FROM patient_sessions
            WHERE session_id = ?
            ORDER BY timestamp ASC
            """,
            (session_id,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_asked_question_ids(session_id: str, db_path: Path | None = None) -> set[str]:
    """Retrieve set of question IDs already asked in a given session."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT question_asked_id
            FROM patient_sessions
            WHERE session_id = ? AND question_asked_id IS NOT NULL AND question_asked_id != ''
            """,
            (session_id,),
        )
        rows = cursor.fetchall()
        return {row["question_asked_id"] for row in rows}


def get_questions_by_level(
    system: str,
    level: str,
    exclude_ids: set[str] | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Query questions_bank using parameterized SQL for given system and level, excluding already asked."""
    exclude = set(exclude_ids or set())
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            query = f"""
                SELECT id, system, level, text, rationale, jev_primitive, trigger_condition
                FROM questions_bank
                WHERE system = ? AND level = ? AND id NOT IN ({placeholders})
                ORDER BY id ASC
            """
            params = [system, level] + list(exclude)
            cursor.execute(query, params)
        else:
            query = """
                SELECT id, system, level, text, rationale, jev_primitive, trigger_condition
                FROM questions_bank
                WHERE system = ? AND level = ?
                ORDER BY id ASC
            """
            cursor.execute(query, (system, level))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_all_bank_questions(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Fetch all questions currently in questions_bank."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, system, level, text, rationale, jev_primitive, trigger_condition FROM questions_bank ORDER BY level, id")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_level_1_screening_questions(
    exclude_ids: set[str] | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve Level 1 screening questions across all body systems, excluding already asked."""
    exclude = set(exclude_ids or set())
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            query = f"""
                SELECT id, system, level, text, rationale, jev_primitive, trigger_condition
                FROM questions_bank
                WHERE level = '1_Chief_Complaint' AND id NOT IN ({placeholders})
                ORDER BY id ASC
            """
            cursor.execute(query, list(exclude))
        else:
            query = """
                SELECT id, system, level, text, rationale, jev_primitive, trigger_condition
                FROM questions_bank
                WHERE level = '1_Chief_Complaint'
                ORDER BY id ASC
            """
            cursor.execute(query)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_question_by_id(question_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """Retrieve a single question by primary key id."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, system, level, text, rationale, jev_primitive, trigger_condition
            FROM questions_bank
            WHERE id = ?
            """,
            (question_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

