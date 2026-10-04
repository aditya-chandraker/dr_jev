# NOTES

Draft notes for the TypeSafe MVP.

## Confirmed from docs

- `NoulAnswer` fields: `noul` only.
- `ChoiceAnswer` fields: `choice`, `probabilities`, `confidence`.
- `ScoreAnswer` fields: `score`, `probabilities`, `legend`, `confidence`.
- `Noul.noul` is the probability of a yes/true answer, from `0` to `1`.
- `Choice.confidence` is reported on a `0` to `1` scale.
- `Score.score` is the probability-weighted average of the rubric levels and may fall between integer levels.
- `Score.confidence` is reported on a `0` to `1` scale.
- The SDK response object exposes grouped answers at `response.nouls`, `response.choices`, and `response.scores`, and also a unified `response.answers` mapping.
- Model selection can be done when constructing the client, for example `TypeSafeClient(model="jev-latest")` or `AsyncTypeSafeClient(model="jev-latest")`.
- Rate limits for Jev 1.13 are documented as `100K tokens per second / 80 requests per second`.

## Confirmed by live smoke test

- `TypeSafeClient(model="jev-latest")` and `AsyncTypeSafeClient(model="jev-latest")` resolve to `jev-1.13.0` in this account.
- A smoke request with one `Noul`, one `Choice`, and one `Score` returned grouped answers under `response.nouls`, `response.choices`, `response.scores`, and `response.answers`.
- The smoke request reported `request_id`, `model`, and `usage` metadata on the response object.
- The live API accepted 50, 100, 200, and 400 questions in a single request in this environment.
- The current default `MAX_QUESTIONS_PER_REQUEST` is `400`, so the MVP sends the whole urinary bank in one call unless overridden.
- The smoke script now checks for the grouped response fields `nouls`, `choices`, `scores`, and `answers`, then prints the grouped keys explicitly.

## Live test summary

- `pytest` passed with 12 tests total, including the live scenario smoke checks.
- The live demo behavior matched the intended tendencies for trouble-peeing, visible blood, retention, fever/dysuria, and unrelated complaints falling back to the standard checklist.

## Unconfirmed

- Maximum number of questions per request: `UNCONFIRMED`.
- Exact retry behavior under every API failure mode: `UNCONFIRMED` beyond the documented SDK retry policy.
