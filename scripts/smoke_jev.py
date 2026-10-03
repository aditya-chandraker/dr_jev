from __future__ import annotations

import asyncio
import os
import time

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score


load_dotenv()


async def main() -> None:
    if not os.getenv("TYPESAFE_API_KEY"):
        print("TYPESAFE_API_KEY is not set; skipping live smoke test.")
        return

    start = time.perf_counter()
    async with AsyncTypeSafeClient(model=os.getenv("TYPESAFE_MODEL", "jev-latest")) as client:
        response = await client.system_one(
            state={"patient_statements": ["I am having trouble peeing"], "demographics": {"age": 58, "sex": "male"}},
            questions={
                "billing": Noul(instructions="Is this about billing?"),
                "tone": Choice(
                    instructions="What is the customer's tone?",
                    criteria={"calm": None, "frustrated": None, "angry": None},
                ),
                "urgency": Score(
                    instructions="How urgent is this situation?",
                    criteria=["can wait", "this week", "today"],
                ),
            },
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    print("elapsed_ms", elapsed_ms)
    print("model", response.model)
    print("request_id", response.request_id)
    print("answers", response.answers)
    print("nouls", response.nouls)
    print("choices", response.choices)
    print("scores", response.scores)


if __name__ == "__main__":
    asyncio.run(main())
