from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BANK_PATH = ROOT / "data" / "urinary_bank.json"


@dataclass(frozen=True)
class BankQuestion:
    id: str
    text: str
    area: str
    rationale: str
    tags: list[str]
    status: str


@dataclass(frozen=True)
class RedFlag:
    id: str
    label: str
    detection_instructions: str
    prompt_question_id: str


@dataclass(frozen=True)
class Bank:
    drafted_by: str
    questions: list[BankQuestion]
    red_flags: list[RedFlag]
    fallback_checklist: list[str]


def load_bank(path: Path = BANK_PATH) -> Bank:
    payload = json.loads(path.read_text())
    questions = [BankQuestion(**item) for item in payload["questions"]]
    red_flags = [RedFlag(**item) for item in payload["red_flags"]]
    fallback_checklist = list(payload["fallback_checklist"])
    bank = Bank(
        drafted_by=payload["drafted_by"],
        questions=questions,
        red_flags=red_flags,
        fallback_checklist=fallback_checklist,
    )
    validate_bank(bank)
    return bank


def validate_bank(bank: Bank) -> None:
    question_ids = [question.id for question in bank.questions]
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("Question IDs must be unique")

    if any(not question.text.strip() for question in bank.questions):
        raise ValueError("Question text cannot be empty")

    if any(not question.area.strip() for question in bank.questions):
        raise ValueError("Question area cannot be empty")

    if any(not question.rationale.strip() for question in bank.questions):
        raise ValueError("Question rationale cannot be empty")

    if any(not question.status.strip() for question in bank.questions):
        raise ValueError("Question status cannot be empty")

    if len(bank.fallback_checklist) != len(set(bank.fallback_checklist)):
        raise ValueError("Fallback checklist IDs must be unique")

    question_id_set = set(question_ids)
    for ref_id in bank.fallback_checklist:
        if ref_id not in question_id_set:
            raise ValueError(f"Fallback checklist references missing question: {ref_id}")

    for red_flag in bank.red_flags:
        if red_flag.prompt_question_id not in question_id_set:
            raise ValueError(f"Red flag references missing question: {red_flag.prompt_question_id}")
        if not red_flag.label.strip():
            raise ValueError("Red flag label cannot be empty")
        if not red_flag.detection_instructions.strip():
            raise ValueError("Red flag detection instructions cannot be empty")


def get_question_map(bank: Bank) -> dict[str, BankQuestion]:
    return {question.id: question for question in bank.questions}

