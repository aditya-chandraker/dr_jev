from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ROSTER_PATH = Path(__file__).resolve().parent.parent / "data" / "patients.json"


@lru_cache(maxsize=1)
def load_roster(path: Path = ROSTER_PATH) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        patients = json.load(handle)["patients"]
    return {patient["id"]: patient for patient in patients}


def roster_summary(roster: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": patient["id"],
            "name": patient["name"],
            "source": "finchnode" if patient.get("finchnode_subject") else "local",
        }
        for patient in sorted(roster.values(), key=lambda item: item["name"])
    ]
