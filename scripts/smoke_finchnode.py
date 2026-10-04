from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.finchnode import FINCHNODE_SCENARIO, FinchNodeClient, summarize_records  # noqa: E402


async def main() -> None:
    client = FinchNodeClient.from_env()
    if client is None:
        print("FINCHNODE_API_KEY is not set; skipping live smoke test.")
        return

    start = time.perf_counter()
    try:
        app_info = await client.get_app()
        print("app", app_info.get("name"), app_info.get("environment"), app_info.get("categories"))
        session = await client.create_simulated_session(scenario=FINCHNODE_SCENARIO)
        print("session", session["id"], "simulation", session.get("simulation"))
        session = await client.wait_for_simulation(session["id"])
        print("subject", session["subject"], "organization", session.get("organization"))
        snapshot = await client.get_records(session["subject"])
    finally:
        await client.aclose()

    summary = summarize_records(snapshot)
    print("elapsed_ms", int((time.perf_counter() - start) * 1000))
    print("demographics", summary["demographics"])
    print("sources", summary["sources"])
    for line in summary["health_record"]:
        print(" -", line)


if __name__ == "__main__":
    asyncio.run(main())
