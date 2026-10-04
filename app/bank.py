from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANK_DIR = Path(os.getenv("BANK_DIR", str(ROOT / "data" / "banks")))
MANIFEST_NAME = "manifest.json"
GENERAL_BANK_ID = "general"
ID_SEP = ":"


def qualify(bank_id: str, item_id: str) -> str:
    return f"{bank_id}{ID_SEP}{item_id}"


@dataclass(frozen=True)
class BankQuestion:
    id: str
    text: str
    area: str
    rationale: str
    tags: list[str]
    status: str
    bank_id: str = ""

    @property
    def qid(self) -> str:
        return qualify(self.bank_id, self.id) if self.bank_id else self.id


@dataclass(frozen=True)
class RedFlag:
    id: str
    label: str
    detection_instructions: str
    prompt_question_id: str
    bank_id: str = ""

    @property
    def qid(self) -> str:
        return qualify(self.bank_id, self.id) if self.bank_id else self.id

    @property
    def prompt_qid(self) -> str:
        return qualify(self.bank_id, self.prompt_question_id) if self.bank_id else self.prompt_question_id


@dataclass(frozen=True)
class Bank:
    bank_id: str
    drafted_by: str
    questions: list[BankQuestion]
    red_flags: list[RedFlag]
    fallback_checklist: list[str]


@dataclass(frozen=True)
class DomainInfo:
    id: str
    label: str
    file: str
    router_description: str
    keywords: list[str]


@dataclass(frozen=True)
class BankRegistry:
    domains: dict[str, DomainInfo]
    banks: dict[str, Bank]
    general: Bank


def load_bank(path: Path, bank_id: str) -> Bank:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = [BankQuestion(**item, bank_id=bank_id) for item in payload["questions"]]
    red_flags = [RedFlag(**item, bank_id=bank_id) for item in payload["red_flags"]]
    bank = Bank(
        bank_id=bank_id,
        drafted_by=payload["drafted_by"],
        questions=questions,
        red_flags=red_flags,
        fallback_checklist=list(payload["fallback_checklist"]),
    )
    validate_bank(bank)
    return bank


@lru_cache(maxsize=4)
def _load_registry_cached(bank_dir: str) -> BankRegistry:
    base = Path(bank_dir)
    manifest = json.loads((base / MANIFEST_NAME).read_text(encoding="utf-8"))
    domains: dict[str, DomainInfo] = {}
    banks: dict[str, Bank] = {}
    for entry in manifest["domains"]:
        info = DomainInfo(
            id=entry["id"],
            label=entry["label"],
            file=entry["file"],
            router_description=entry["router_description"],
            keywords=[k.lower() for k in entry.get("keywords", [])],
        )
        if not info.id or info.id == GENERAL_BANK_ID or ID_SEP in info.id:
            raise ValueError(f"Invalid domain id: {info.id!r}")
        if info.id in domains:
            raise ValueError(f"Duplicate domain id: {info.id}")
        if not info.router_description.strip():
            raise ValueError(f"Domain {info.id} needs a router_description")
        domains[info.id] = info
        banks[info.id] = load_bank(base / info.file, info.id)
    general = load_bank(base / manifest["general_bank"], GENERAL_BANK_ID)
    if not general.fallback_checklist:
        raise ValueError("General bank needs a non-empty fallback_checklist")
    return BankRegistry(domains=domains, banks=banks, general=general)


def load_registry(bank_dir: Path | str | None = None) -> BankRegistry:
    return _load_registry_cached(str(bank_dir or BANK_DIR))


def validate_bank(bank: Bank) -> None:
    question_ids = [q.id for q in bank.questions]
    if len(question_ids) != len(set(question_ids)):
        raise ValueError(f"[{bank.bank_id}] Question IDs must be unique")
    for q in bank.questions:
        if not q.text.strip():
            raise ValueError(f"[{bank.bank_id}] Question text cannot be empty ({q.id})")
        if not q.area.strip():
            raise ValueError(f"[{bank.bank_id}] Question area cannot be empty ({q.id})")
        if not q.rationale.strip():
            raise ValueError(f"[{bank.bank_id}] Question rationale cannot be empty ({q.id})")
        if not q.status.strip():
            raise ValueError(f"[{bank.bank_id}] Question status cannot be empty ({q.id})")
    if len(bank.fallback_checklist) != len(set(bank.fallback_checklist)):
        raise ValueError(f"[{bank.bank_id}] Fallback checklist IDs must be unique")
    id_set = set(question_ids)
    for ref in bank.fallback_checklist:
        if ref not in id_set:
            raise ValueError(f"[{bank.bank_id}] Fallback checklist references missing question: {ref}")
    rf_ids = [rf.id for rf in bank.red_flags]
    if len(rf_ids) != len(set(rf_ids)):
        raise ValueError(f"[{bank.bank_id}] Red flag IDs must be unique")
    for rf in bank.red_flags:
        if rf.prompt_question_id not in id_set:
            raise ValueError(f"[{bank.bank_id}] Red flag references missing question: {rf.prompt_question_id}")
        if not rf.label.strip():
            raise ValueError(f"[{bank.bank_id}] Red flag label cannot be empty")
        if not rf.detection_instructions.strip():
            raise ValueError(f"[{bank.bank_id}] Red flag detection instructions cannot be empty")


def get_question_map(bank: Bank) -> dict[str, BankQuestion]:
    """Keyed by RAW question id, within one bank."""
    return {q.id: q for q in bank.questions}


def get_qualified_question_map(registry: BankRegistry) -> dict[str, BankQuestion]:
    """Keyed by QUALIFIED id across every bank including general."""
    out: dict[str, BankQuestion] = {}
    for bank in [*registry.banks.values(), registry.general]:
        for q in bank.questions:
            out[q.qid] = q
    return out
