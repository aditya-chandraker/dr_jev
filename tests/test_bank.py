from app.bank import load_bank, validate_bank


def test_bank_validates_cleanly() -> None:
    bank = load_bank()
    validate_bank(bank)


def test_bank_has_unique_ids_and_references() -> None:
    bank = load_bank()
    question_ids = [question.id for question in bank.questions]
    assert len(question_ids) == len(set(question_ids))
    question_id_set = set(question_ids)
    assert all(ref in question_id_set for ref in bank.fallback_checklist)
    assert all(flag.prompt_question_id in question_id_set for flag in bank.red_flags)


def test_bank_has_required_fields() -> None:
    bank = load_bank()
    for question in bank.questions:
        assert question.id.strip()
        assert question.text.strip()
        assert question.area.strip()
        assert question.rationale.strip()
        assert question.status.strip()

