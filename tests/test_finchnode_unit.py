from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.engine import _build_state
from app.finchnode import FinchNodeClient, FinchNodeError, summarize_records
from app.models import PatientInput


SNAPSHOT = {
    "sources": [{"system": "northstar-health", "organization": "Northstar Health System (Synthetic)"}],
    "data": {
        "demographics": {
            "name": "Morgan Rivera",
            "birthDate": "1988-04-17",
            "gender": "female",
            "phone": "+1-202-555-0142",
            "email": "morgan.rivera@example.test",
        },
        "conditions": [{"name": "Type 2 diabetes mellitus", "status": "active", "onsetDate": "2021-03-12"}],
        "medications": [
            {"name": "Metformin 500 mg tablet", "status": "active", "frequency": "Twice daily", "reason": "Type 2 diabetes mellitus"}
        ],
        "allergies": [{"substance": "Penicillin", "reaction": "Synthetic example: rash", "severity": "mild"}],
        "labs": [
            {"name": "Hemoglobin A1c", "value": "6.1", "unit": "%", "date": "2026-01-10T15:30:00Z"},
            {"name": "Hemoglobin A1c", "value": "6.4", "unit": "%", "interpretation": "H", "date": "2026-07-18T15:30:00Z"},
        ],
    },
}


def _session(state: str, subject: str | None = None, sync: str = "partial") -> dict[str, object]:
    return {
        "id": "cs_test",
        "status": "completed" if subject else "system-selected",
        "subject": subject,
        "organization": "Northstar Health System (Synthetic)",
        "sync": {"status": sync},
        "simulation": {"scenario": "baseline-adult", "state": state},
    }


def _client(handler) -> FinchNodeClient:
    http = httpx.AsyncClient(base_url="https://finchnode.test/api/v1", transport=httpx.MockTransport(handler))
    return FinchNodeClient("ck_test_unit", http=http)


def test_summarize_records_keeps_clinical_facts_and_drops_identifiers() -> None:
    summary = summarize_records(SNAPSHOT, today=date(2026, 10, 4))

    assert summary["demographics"] == {"age": 38, "sex": "female"}
    assert summary["sources"] == ["Northstar Health System (Synthetic)"]
    assert summary["health_record"] == [
        "Condition (active): Type 2 diabetes mellitus (since 2021-03-12)",
        "Medication (active): Metformin 500 mg tablet, Twice daily, for Type 2 diabetes mellitus",
        "Allergy: Penicillin (rash, mild)",
        "Lab: Hemoglobin A1c 6.4 % (H) on 2026-07-18",
    ]
    flattened = json.dumps(summary)
    for identifier in ["Morgan", "555-0142", "example.test", "1988-04-17"]:
        assert identifier not in flattened


@pytest.mark.asyncio
async def test_wait_for_simulation_returns_completed_session() -> None:
    responses = iter([_session("syncing"), _session("completed", subject="u_0123456789abcdef")])
    client = _client(lambda request: httpx.Response(200, json=next(responses)))

    session = await client.wait_for_simulation("cs_test", poll_s=0, resume_after_s=60)

    assert session["subject"] == "u_0123456789abcdef"
    await client.aclose()


@pytest.mark.asyncio
async def test_wait_for_simulation_resumes_stalled_simulation() -> None:
    calls: list[tuple[str, str]] = []
    gets = iter([_session("syncing"), _session("syncing"), _session("completed", subject="u_0123456789abcdef")])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content) == {"scenario": "baseline-adult"}
            return httpx.Response(202, json=_session("syncing"))
        return httpx.Response(200, json=next(gets))

    client = _client(handler)
    await client.wait_for_simulation("cs_test", poll_s=0, resume_after_s=0)

    assert ("POST", "/api/v1/connect/sessions/cs_test/simulate") in calls
    await client.aclose()


@pytest.mark.asyncio
async def test_wait_for_simulation_raises_on_failure() -> None:
    failed = _session("failed")
    failed["simulation"]["failureCode"] = "source_unavailable"
    client = _client(lambda request: httpx.Response(200, json=failed))

    with pytest.raises(FinchNodeError, match="source_unavailable"):
        await client.wait_for_simulation("cs_test", poll_s=0)
    await client.aclose()


@pytest.mark.asyncio
async def test_api_errors_surface_code_and_message() -> None:
    body = {"error": {"code": "sandbox_limit_reached", "message": "Too many synthetic patients"}}
    client = _client(lambda request: httpx.Response(409, json=body))

    with pytest.raises(FinchNodeError) as excinfo:
        await client.simulate("cs_test")
    assert (excinfo.value.status, excinfo.value.code) == (409, "sandbox_limit_reached")
    await client.aclose()


def test_create_session_endpoint_triggers_simulation_immediately() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/simulate"):
            return httpx.Response(202, json=_session("syncing"))
        return httpx.Response(201, json={"id": "cs_test", "status": "pending", "subject": None})

    with TestClient(main.app) as http:
        main.app.state.finchnode_client = _client(handler)
        response = http.post("/api/finchnode/sessions", json={"scenario": "baseline-adult"})

    assert response.status_code == 201
    assert response.json()["simulation"] == {"scenario": "baseline-adult", "state": "syncing"}
    assert calls == [
        ("POST", "/api/v1/connect/sessions"),
        ("POST", "/api/v1/connect/sessions/cs_test/simulate"),
    ]


def test_patient_by_subject_reads_records_directly(monkeypatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=SNAPSHOT)

    monkeypatch.setattr(main, "FINCHNODE_SUBJECT", "u_fromenv")
    with TestClient(main.app) as http:
        main.app.state.finchnode_client = _client(handler)
        explicit = http.get("/api/finchnode/patient", params={"subject": "u_0123456789abcdef"})
        from_env = http.get("/api/finchnode/patient")

    assert explicit.status_code == 200
    assert explicit.json()["subject"] == "u_0123456789abcdef"
    assert explicit.json()["session_id"] is None
    assert "Allergy: Penicillin (rash, mild)" in explicit.json()["health_record"]
    assert from_env.json()["subject"] == "u_fromenv"
    assert paths == ["/api/v1/users/u_0123456789abcdef/records", "/api/v1/users/u_fromenv/records"]


def test_patient_by_subject_requires_valid_subject(monkeypatch) -> None:
    monkeypatch.setattr(main, "FINCHNODE_SUBJECT", "")
    with TestClient(main.app) as http:
        main.app.state.finchnode_client = _client(lambda request: httpx.Response(500))
        missing = http.get("/api/finchnode/patient")
        invalid = http.get("/api/finchnode/patient", params={"subject": "../app"})

    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "no_subject")
    assert (invalid.status_code, invalid.json()["detail"]["code"]) == (422, "invalid_subject")


def test_build_state_includes_health_record_only_when_present() -> None:
    assert "health_record" not in _build_state(PatientInput(patient_statements=["hi"]))
    state = _build_state(PatientInput(patient_statements=["hi"], health_record=["Allergy: Penicillin"]))
    assert state["health_record"] == ["Allergy: Penicillin"]
