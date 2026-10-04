from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import date
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv()

LOGGER = logging.getLogger(__name__)
FINCHNODE_BASE_URL = os.getenv("FINCHNODE_BASE_URL", "https://api.finchnode.com/api/v1")
FINCHNODE_SCENARIO = os.getenv("FINCHNODE_SCENARIO", "baseline-adult")
FINCHNODE_SUBJECT = os.getenv("FINCHNODE_SUBJECT", "").strip()
SUBJECT_PATTERN = r"^u_[A-Za-z0-9]+$"
SIMULATION_TIMEOUT_S = float(os.getenv("FINCHNODE_SIMULATION_TIMEOUT_S", "90"))
SIMULATION_POLL_S = float(os.getenv("FINCHNODE_SIMULATION_POLL_S", "2"))
SIMULATION_RESUME_AFTER_S = float(os.getenv("FINCHNODE_SIMULATION_RESUME_AFTER_S", "20"))
MAX_LABS = 10
TERMINAL_SYNC_STATUSES = {"completed", "partial"}


class FinchNodeError(Exception):
    def __init__(self, status: int | None, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class FinchNodeClient:
    def __init__(self, api_key: str, base_url: str = FINCHNODE_BASE_URL, http: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("FinchNode API key is required")
        self.is_sandbox = api_key.startswith("ck_test_")
        self._http = http or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    @classmethod
    def from_env(cls) -> FinchNodeClient | None:
        api_key = os.getenv("FINCHNODE_API_KEY", "").strip()
        return cls(api_key) if api_key else None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._http.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            return body
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code, message = error.get("code", "api_error"), error.get("message", response.text)
        elif isinstance(body, dict):
            code, message = body.get("code") or str(error or "api_error"), body.get("message", response.text)
        else:
            code, message = "api_error", response.text
        raise FinchNodeError(response.status_code, str(code), str(message))

    async def get_app(self) -> dict[str, Any]:
        return await self._request("GET", "/app")

    async def create_session(self, *, external_id: str | None = None, categories: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"externalId": external_id or f"drjev_{uuid.uuid4().hex[:12]}"}
        if categories:
            body["categories"] = categories
        return await self._request("POST", "/connect/sessions", json=body)

    async def simulate(self, session_id: str, scenario: str = FINCHNODE_SCENARIO) -> dict[str, Any]:
        if not self.is_sandbox:
            raise FinchNodeError(None, "sandbox_only", "Simulation requires a ck_test_ sandbox key")
        return await self._request("POST", f"/connect/sessions/{session_id}/simulate", json={"scenario": scenario})

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/connect/sessions/{session_id}")

    async def get_records(self, subject: str, categories: list[str] | None = None) -> dict[str, Any]:
        params = {"categories": ",".join(categories)} if categories else None
        return await self._request("GET", f"/users/{subject}/records", params=params)

    async def create_simulated_session(
        self,
        *,
        scenario: str = FINCHNODE_SCENARIO,
        external_id: str | None = None,
        categories: list[str] | None = None,
    ) -> dict[str, Any]:
        session = await self.create_session(external_id=external_id, categories=categories)
        return await self.simulate(session["id"], scenario)

    async def wait_for_simulation(
        self,
        session_id: str,
        *,
        timeout_s: float = SIMULATION_TIMEOUT_S,
        poll_s: float = SIMULATION_POLL_S,
        resume_after_s: float = SIMULATION_RESUME_AFTER_S,
    ) -> dict[str, Any]:
        start = last_resume = time.monotonic()
        while True:
            session = await self.get_session(session_id)
            simulation = session.get("simulation") or {}
            state = simulation.get("state")
            if state == "completed" and session.get("subject"):
                return session
            if state == "failed":
                raise FinchNodeError(None, simulation.get("failureCode") or "simulation_failed", "Sandbox simulation failed")
            now = time.monotonic()
            if now - start >= timeout_s:
                raise TimeoutError(f"Simulation for {session_id} did not complete within {timeout_s:.0f}s (state={state})")
            sync_status = (session.get("sync") or {}).get("status")
            if simulation.get("scenario") and sync_status in TERMINAL_SYNC_STATUSES and now - last_resume >= resume_after_s:
                LOGGER.info("Resuming stalled FinchNode simulation %s (state=%s, sync=%s)", session_id, state, sync_status)
                last_resume = now
                try:
                    await self.simulate(session_id, simulation["scenario"])
                except FinchNodeError as exc:
                    LOGGER.info("Resume of %s refused: %s", session_id, exc)
            await asyncio.sleep(poll_s)


def _age_from_birth_date(birth_date: str | None, today: date | None = None) -> int | None:
    if not birth_date:
        return None
    try:
        born = date.fromisoformat(birth_date[:10])
    except ValueError:
        return None
    today = today or date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _clean(text: Any) -> str:
    return str(text or "").replace("Synthetic example:", "").strip()


def _join(*parts: Any) -> str:
    return ", ".join(_clean(part) for part in parts if _clean(part))


def summarize_records(snapshot: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Reduce a records snapshot to age, sex, and one-line chart facts. Identifiers such as
    name, contact details, and exact birth date are deliberately dropped."""
    data = snapshot.get("data") or {}
    demographics: dict[str, Any] = {}
    patient = data.get("demographics") or {}
    if isinstance(patient, list):
        patient = patient[0] if patient else {}
    age = _age_from_birth_date(patient.get("birthDate"), today)
    if age is not None:
        demographics["age"] = age
    if patient.get("gender"):
        demographics["sex"] = patient["gender"]

    lines: list[str] = []
    for condition in data.get("conditions") or []:
        since = f" (since {condition['onsetDate'][:10]})" if condition.get("onsetDate") else ""
        lines.append(f"Condition ({condition.get('status') or 'unknown'}): {_clean(condition.get('name'))}{since}")
    for medication in data.get("medications") or []:
        detail = _join(medication.get("frequency"), f"for {medication['reason']}" if medication.get("reason") else "")
        lines.append(f"Medication ({medication.get('status') or 'unknown'}): {_clean(medication.get('name'))}" + (f", {detail}" if detail else ""))
    for allergy in data.get("allergies") or []:
        detail = _join(allergy.get("reaction"), allergy.get("severity"))
        lines.append(f"Allergy: {_clean(allergy.get('substance') or allergy.get('name'))}" + (f" ({detail})" if detail else ""))
    labs = sorted(data.get("labs") or [], key=lambda lab: lab.get("date") or "", reverse=True)
    seen_labs: set[str] = set()
    for lab in labs:
        name = _clean(lab.get("name"))
        if not name or name in seen_labs:
            continue
        seen_labs.add(name)
        value = " ".join(part for part in [str(lab.get("value") or ""), str(lab.get("unit") or "")] if part)
        flag = f" ({lab['interpretation']})" if lab.get("interpretation") else ""
        when = f" on {lab['date'][:10]}" if lab.get("date") else ""
        lines.append(f"Lab: {name} {value}{flag}{when}".replace("  ", " "))
        if len(seen_labs) >= MAX_LABS:
            break

    sources = sorted({str(source.get("organization") or source.get("system")) for source in snapshot.get("sources") or [] if isinstance(source, dict)})
    return {"demographics": demographics, "health_record": lines, "sources": sources}
