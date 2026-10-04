from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.bank import BANK_DIR, GENERAL_BANK_ID, load_bank, load_registry, validate_bank


def test_registry_loads_all_banks() -> None:
    registry = load_registry()
    assert set(registry.domains) == {
        "urinary",
        "respiratory",
        "oral",
        "musculoskeletal",
        "head_neck",
        "digestive",
        "cardiovascular",
        "neurological",
        "dermatological",
        "psychiatric",
        "gynecologic",
        "endocrine",
        "ophthalmologic",
        "constitutional",
        "hematologic",
        "allergic",
        "breast",
        "male_genital",
        "sleep",
        "obstetric",
        "family_history",
        "social_history",
        "sexual_history",
    }
    assert registry.general.bank_id == GENERAL_BANK_ID
    for bank in [*registry.banks.values(), registry.general]:
        validate_bank(bank)


def test_qualified_ids_are_unique_and_statuses_are_preserved() -> None:
    registry = load_registry()
    qids = []
    rf_qids = []
    for bank in [*registry.banks.values(), registry.general]:
        qids.extend(question.qid for question in bank.questions)
        rf_qids.extend(red_flag.qid for red_flag in bank.red_flags)
        assert all(question.status == "draft-needs-clinician-review" for question in bank.questions)
    assert len(qids) == len(set(qids))
    assert len(rf_qids) == len(set(rf_qids))


def test_manifest_and_files_are_consistent() -> None:
    registry = load_registry()
    expected_files = {
        "manifest.json",
        "_general.json",
        "urinary.json",
        "respiratory.json",
        "oral.json",
        "musculoskeletal.json",
        "head_neck.json",
        "digestive.json",
        "cardiovascular.json",
        "neurological.json",
        "dermatological.json",
        "psychiatric.json",
        "gynecologic.json",
        "endocrine.json",
        "ophthalmologic.json",
        "constitutional.json",
        "hematologic.json",
        "allergic.json",
        "breast.json",
        "male_genital.json",
        "sleep.json",
        "obstetric.json",
        "family_history.json",
        "social_history.json",
        "sexual_history.json",
    }
    assert {path.name for path in BANK_DIR.glob("*.json")} == expected_files
    for domain in registry.domains.values():
        assert domain.router_description.strip()
        assert len(domain.keywords) >= 3


def test_corrupt_bank_raises_with_bank_id(tmp_path: Path) -> None:
    source = BANK_DIR / "urinary.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["questions"][1]["id"] = payload["questions"][0]["id"]
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[bad_bank\]"):
        load_bank(bad_file, "bad_bank")


def test_missing_fallback_reference_raises_with_bank_id(tmp_path: Path) -> None:
    source = BANK_DIR / "urinary.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["fallback_checklist"][0] = "q_missing_question"
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[bad_bank\]"):
        load_bank(bad_file, "bad_bank")
