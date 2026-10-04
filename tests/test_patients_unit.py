from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app import main
from app.finchnode import FinchNodeClient
from app.patients import load_roster
from tests.test_finchnode_unit import SNAPSHOT


def test_roster_entries_are_complete_and_unique() -> None:
    roster = load_roster()
    names = [patient["name"] for patient in roster.values()]

    assert len(roster) >= 10
    assert len(set(names)) == len(names)
    for patient in roster.values():
        if patient.get("finchnode_subject"):
            continue
        assert patient["demographics"]["age"] > 0
        assert patient["demographics"]["sex"] in {"female", "male"}
        assert patient["health_record"]
        assert "(Synthetic)" in patient["organization"]


def test_patients_endpoint_lists_names_sorted() -> None:
    with TestClient(main.app) as http:
        body = http.get("/api/patients").json()

    names = [patient["name"] for patient in body]
    assert names == sorted(names)
    assert {"id", "name", "source"} == set(body[0])
    assert any(patient["source"] == "finchnode" for patient in body)


def test_local_patient_returns_stored_chart() -> None:
    with TestClient(main.app) as http:
        chart = http.get("/api/patients/samuel-okafor").json()
        missing = http.get("/api/patients/nobody")

    assert chart["name"] == "Samuel Okafor"
    assert chart["subject"] is None
    assert chart["demographics"] == {"age": 72, "sex": "male"}
    assert "Lab: eGFR 42 mL/min/1.73m2 (L) on 2026-07-30" in chart["health_record"]
    assert missing.status_code == 404


def test_finchnode_patient_reads_live_records_with_roster_name() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=SNAPSHOT)

    http_client = httpx.AsyncClient(base_url="https://finchnode.test/api/v1", transport=httpx.MockTransport(handler))
    with TestClient(main.app) as http:
        main.app.state.finchnode_client = FinchNodeClient("ck_test_unit", http=http_client)
        chart = http.get("/api/patients/morgan-rivera").json()

    assert chart["name"] == "Morgan Rivera"
    assert chart["subject"] == "u_bb6bb97dbf280606"
    assert paths == ["/api/v1/users/u_bb6bb97dbf280606/records"]
