# Patient Interview Helper MVP

Hackathon demo for clinician training and practice with simulated patients only.

## What it is

The app uses TypeSafe Jev to rank clinician-authored urinary-symptom questions. It never generates new question text. The backend chooses from the question bank, prioritizes red flags, and falls back to a static checklist when confidence is low.

## Setup

This repo is intended to run from the existing local virtual environment in `.venv`.

1. Activate the virtual environment:

```bash
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` from `.env.example` and set `TYPESAFE_API_KEY`:

```bash
cp .env.example .env
```

## Speech to text (optional)

Click "Record conversation" in the UI and run the interview as usual.

In Chrome and Edge, the browser's built-in speech recognition turns speech into text as it is spoken (no audio is uploaded to this server; the browser sends it to its own speech service). Each finished utterance then goes to:

1. `POST /api/speaker`, which asks Jev (a TypeSafe `Choice`, using the last six labeled utterances as context) whether the physician, the patient, or someone else said it. This takes about 0.1 to 0.3 seconds; without `TYPESAFE_API_KEY` it falls back to a rule (a question mark means physician).
2. For patient utterances, `POST /api/simplify`, which sends only the text and the physician's preceding question to Gemini (`GEMINI_LIVE_MODEL` first) for a short plain-language restatement that keeps every clinical detail, including what the patient denies. This takes about 1 to 2 seconds and needs `GEMINI_API_KEY`.

The patient's exact words appear in the "Patient statement" box about half a second after they finish speaking and are replaced by the simplified version when it arrives. If simplification fails (for example, Gemini quota is exhausted), the exact words stay.

In browsers without speech recognition (such as Firefox), recording falls back to Gemini audio transcription (recording stops on its own after 3 minutes). The browser converts the audio so far to 16 kHz mono WAV about every 4 seconds and posts it to `POST /api/transcribe`, which:

1. sends the audio to Gemini and asks for a JSON transcript with each turn labeled `physician`, `patient`, or `other` by conversational role, plus a short plain-language restatement of each patient answer (`simplified`) that keeps every clinical detail, including what the patient denies;
2. uses `GEMINI_LIVE_MODEL` (default `gemini-3.5-flash`, about 2 to 3 seconds per pass) while recording and `GEMINI_MODEL` (default `gemini-3.8-flash`) for the final pass, with thinking set to `GEMINI_THINKING_LEVEL` (default `low`);
3. on overload or rate limits, moves to the next model (`GEMINI_MODEL`, then `GEMINI_FALLBACK_MODELS`) and skips the overloaded model for `GEMINI_MODEL_COOLDOWN_S` seconds (default 60). Live passes do not wait between retries; the final pass retries briefly first;
4. returns the turns, the patient's statements, and each physician question paired with the patient's answer.

With either path, as soon as the patient answers, the UI fills the "Patient statement" box with the answer (highlighted). The physician reviews it and clicks "Add statement", which adds the answer to the interview, records the physician's question and the answer under "Asked", and requests new suggestions from Jev. If the physician edits the box, live updates stop overwriting it until the statement is added. With Gemini audio transcription, stopping the recording runs one final pass. Nothing is added without a click. Transcript text is not written to the server logs. Run `python scripts/smoke_transcribe.py path/to/recording.wav` for a live check against a recording of your own.

## FinchNode synthetic patients (optional)

Set `FINCHNODE_API_KEY` in `.env` to a sandbox key (`ck_test_...`). The "Load patient" button in the UI then:

1. calls `POST /api/finchnode/sessions`, which creates a FinchNode Connect session and immediately calls the sandbox `/simulate` endpoint (scenario from `FINCHNODE_SCENARIO`, default `baseline-adult`), so no hosted login is needed;
2. calls `GET /api/finchnode/sessions/{id}/patient`, which waits for the simulation to complete, reads the patient's records, and returns age, sex, and one-line chart facts (conditions, medications, allergies, recent labs);
3. sends those chart facts as `health_record` with each `/api/suggest` request, so Jev can skip questions the chart already answers.

The "Patient" box searches the fictional roster in `data/patients.json` by name; picking a name fills in age, sex, and the health record. Roster entries with a `finchnode_subject` (Morgan Rivera) are read live from FinchNode; the rest are stored synthetic charts in the same one-line format. Every name and record there is fictional.

To skip the simulation and load a patient who has already connected (for example through the session's hosted `url`), set `FINCHNODE_SUBJECT` in `.env` to their patient ID (`u_...`, the session's `subject`). The button then calls `GET /api/finchnode/patient`, which reads that patient's records directly (it also accepts `?subject=...`). With neither set, it falls back to the simulation above.

Names, contact details, and exact birth dates are dropped before anything is sent to Jev. Run `python scripts/smoke_finchnode.py` for a live check.

## Run

Start the app with uvicorn while the virtual environment is active:

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/`.

## Smoke test

Run the live SDK check:

```bash
python scripts/smoke_jev.py
```

## Tests

Run the fast unit suite:

```bash
pytest -m "not slow"
```

## Project layout

- `app/main.py` FastAPI app and routes
- `app/engine.py` TypeSafe query building and ranking logic
- `app/bank.py` question bank loading and validation
- `app/models.py` request and response models
- `app/finchnode.py` FinchNode sandbox client and chart summary
- `app/transcribe.py` Gemini speech-to-text with speaker roles, and text-only answer simplification
- `app/speaker.py` Jev speaker labeling (physician, patient, other) for live utterances
- `app/static/index.html` single-page UI
- `data/urinary_bank.json` drafted question bank, red flags, and fallback checklist
- `scripts/smoke_jev.py` live API smoke test
- `tests/` unit and live tests

## Known limitations

- The question bank is AI-drafted and not clinically validated.
- The tool is for training with simulated patients, not for real clinical decisions.
- Jev's medical ranking quality is unvalidated. The scenario table is a smoke test, not a clinical evaluation.
- Real use would require clinician review, evaluation against expert judgment, and privacy/regulatory review such as HIPAA and clinical decision support rules.
