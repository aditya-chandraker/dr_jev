from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.database import (
    DEFAULT_SEED_PATH,
    get_all_bank_questions,
    get_asked_question_ids,
    get_questions_by_level,
    get_session_history,
    init_db,
    log_patient_session,
    seed_database,
)
from app.engine import route_patient_hierarchical
from app.main import app
from app.models import RoutingRequest


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_db_file = tmp_path / "test_clinical_engine.db"
    monkeypatch.setenv("DATABASE_PATH", str(test_db_file))
    init_db(test_db_file)
    seed_database(DEFAULT_SEED_PATH, test_db_file)
    yield test_db_file


def test_database_init_and_idempotent_seed(tmp_path: Path):
    db_file = tmp_path / "seed_test.db"
    init_db(db_file)
    
    # First seed: inserts all questions from JSON
    inserted_1 = seed_database(DEFAULT_SEED_PATH, db_file)
    assert inserted_1 > 0
    
    all_questions = get_all_bank_questions(db_file)
    assert len(all_questions) == inserted_1
    assert any(q["level"] == "1_Chief_Complaint" for q in all_questions)
    assert any(q["level"] == "2_Characterization" for q in all_questions)
    assert any(q["level"] == "3_Red_Flag" for q in all_questions)
    assert any(q["level"] == "4_ROS" for q in all_questions)

    # Second seed: idempotent INSERT OR IGNORE -> 0 new rows
    inserted_2 = seed_database(DEFAULT_SEED_PATH, db_file)
    assert inserted_2 == 0


def test_parameterized_query_exclusions(tmp_path: Path):
    db_file = tmp_path / "param_test.db"
    init_db(db_file)
    seed_database(DEFAULT_SEED_PATH, db_file)

    l1_all = get_questions_by_level("urinary", "1_Chief_Complaint", db_path=db_file)
    assert len(l1_all) >= 2
    first_id = l1_all[0]["id"]

    # Exclude first question using parameterized SQL
    l1_filtered = get_questions_by_level("urinary", "1_Chief_Complaint", exclude_ids={first_id}, db_path=db_file)
    assert len(l1_filtered) == len(l1_all) - 1
    assert all(q["id"] != first_id for q in l1_filtered)


@pytest.mark.asyncio
async def test_gated_progression_across_levels(temp_db: Path):
    session_id = "test-session-gated-101"

    # Step 1: Vague complaint without clear system -> stays at Level 1 (Chief Complaint)
    req1 = RoutingRequest(
        session_id=session_id,
        chat_history="I feel uncomfortable recently",
        current_system="urinary",
    )
    res1 = await route_patient_hierarchical(req1)
    assert res1.current_level == "1_Chief_Complaint"
    assert res1.next_question is not None
    assert res1.next_question.level == "1_Chief_Complaint"

    # Step 2: Patient answers and provides clear urinary symptom -> Gate 1 satisfied, advances to Level 2
    req2 = RoutingRequest(
        session_id=session_id,
        chat_history="I have severe burning when I urinate and weak stream",
        patient_response="It burns when I pee",
        current_system="urinary",
    )
    res2 = await route_patient_hierarchical(req2)
    assert res2.current_level == "2_Characterization"
    assert res2.next_question is not None
    assert res2.next_question.level == "2_Characterization"

    # Step 3: Characterization is now satisfied -> advances to Level 3 (Red Flag screening)
    req3 = RoutingRequest(
        session_id=session_id,
        chat_history="The burning is sharp and happens at the end of urinating",
        patient_response="The burning started two days ago",
        current_system="urinary",
    )
    res3 = await route_patient_hierarchical(req3)
    assert res3.current_level == "3_Red_Flag"
    assert res3.next_question is not None
    assert res3.next_question.level == "3_Red_Flag"

    # Step 4: Red flags screened -> advances to Level 4 (Review of Systems)
    req4 = RoutingRequest(
        session_id=session_id,
        chat_history="No blood in urine and no fever",
        patient_response="No blood",
        current_system="urinary",
    )
    res4 = await route_patient_hierarchical(req4)
    assert res4.current_level in ["1_Chief_Complaint", "4_ROS", "Completed"]
    assert res4.next_question is not None or res4.completed


@pytest.mark.asyncio
async def test_red_flag_alert_trigger(temp_db: Path):
    session_id = "test-red-flag-emergency"

    # Patient has urgent acute retention
    req = RoutingRequest(
        session_id=session_id,
        chat_history="I cannot pee at all and my bladder is full and extremely painful",
        current_system="urinary",
        current_level="3_Red_Flag",
    )
    res = await route_patient_hierarchical(req)
    assert res.current_level == "3_Red_Flag"
    assert res.is_red_flag is True
    assert res.next_question.id == "rf_urinary_retention"


@pytest.mark.asyncio
async def test_audit_logging_in_patient_sessions(temp_db: Path):
    session_id = "test-audit-audit-007"
    req = RoutingRequest(
        session_id=session_id,
        chat_history="Pink urine and burning pain",
        current_system="urinary",
    )
    res = await route_patient_hierarchical(req)
    assert res.next_question is not None

    # Verify audit log in patient_sessions
    logs = get_session_history(session_id, db_path=temp_db)
    assert len(logs) == 1
    assert logs[0]["session_id"] == session_id
    assert logs[0]["question_asked_id"] == res.next_question.id
    assert logs[0]["jev_confidence_score"] == res.confidence
    assert logs[0]["timestamp"] is not None


def test_fastapi_endpoints(temp_db: Path):
    client = TestClient(app)

    # 1. Health check
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    # 2. Hierarchical bank listing
    res_bank = client.get("/api/bank/hierarchical")
    assert res_bank.status_code == 200
    data_bank = res_bank.json()
    assert data_bank["count"] > 0

    # 3. Route endpoint
    route_payload = {
        "session_id": "api-client-session-1",
        "chat_history": "I noticed pink urine and trouble starting the stream",
        "current_system": "urinary",
    }
    res_route = client.post("/api/route", json=route_payload)
    assert res_route.status_code == 200
    data_route = res_route.json()
    assert data_route["session_id"] == "api-client-session-1"
    assert "next_question" in data_route
    assert data_route["confidence"] > 0

    # 4. Session audit history endpoint
    res_audit = client.get("/api/sessions/api-client-session-1")
    assert res_audit.status_code == 200
    data_audit = res_audit.json()
    assert data_audit["count"] >= 1
    assert data_audit["logs"][0]["session_id"] == "api-client-session-1"


@pytest.mark.asyncio
async def test_complete_history_adaptive_drilldown(temp_db: Path):
    session_id = "test-session-complete-history-999"

    # Turn 1: Initial screen -> starts with Level 1 screening (General / Constitutional)
    req1 = RoutingRequest(
        session_id=session_id,
        chat_history="I'm here for a routine complete health checkup",
    )
    res1 = await route_patient_hierarchical(req1)
    assert res1.current_level == "1_Chief_Complaint"
    assert res1.drilldown_active is False

    # Turn 2: Patient answers normal ("No fever, no chills, feel fine") -> stays at Level 1, advances to next system
    req2 = RoutingRequest(
        session_id=session_id,
        chat_history="No fever or chills, feeling fine",
        patient_response="No fever or chills, feeling fine",
    )
    res2 = await route_patient_hierarchical(req2)
    assert res2.current_level == "1_Chief_Complaint"
    assert res2.drilldown_active is False
    assert res2.current_system != res1.current_system

    # Turn 3: Patient reports an abnormality in respiratory ("I've had a bad persistent cough producing yellow phlegm")
    # -> Engine MUST branch into Level 2 Characterization specifically for respiratory!
    req3 = RoutingRequest(
        session_id=session_id,
        chat_history="I've had a bad persistent cough producing yellow phlegm and wheezing",
        patient_response="I've had a bad persistent cough producing yellow phlegm and wheezing",
    )
    res3 = await route_patient_hierarchical(req3)
    assert res3.current_level == "2_Characterization"
    assert res3.current_system == "respiratory"
    assert res3.drilldown_active is True
    assert "respiratory" in res3.abnormal_systems


