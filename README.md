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

Set `GEMINI_API_KEY` in `.env`. Click "Record conversation" in the UI and run the interview as usual (recording stops on its own after 3 minutes). While recording, the browser converts the audio so far to 16 kHz mono WAV about every 4 seconds and posts it to `POST /api/transcribe`, which:

1. sends the audio to Gemini (`GEMINI_MODEL`, default `gemini-3.8-flash`) and asks for a JSON transcript with each turn labeled `physician`, `patient`, or `other` by conversational role;
2. retries briefly on overload or rate limits, then tries `GEMINI_FALLBACK_MODELS` (default `gemini-3.5-flash,gemini-flash-latest`);
3. returns the turns, the patient's statements, and each physician question paired with the patient's answer.

As soon as the patient answers, the UI fills the "Patient statement" box with that answer (highlighted) and keeps refining it on each pass. The physician reviews it and clicks "Add statement", which adds the answer to the interview, records the physician's question and the answer under "Asked", and requests new suggestions from Jev. If the physician edits the box, live updates stop overwriting it until the statement is added. Stopping the recording runs one final transcription pass; nothing is added without a click. Transcript text is not written to the server logs. Run `python scripts/smoke_transcribe.py path/to/recording.wav` for a live check against a recording of your own.

## FinchNode synthetic patients (optional)

Set `FINCHNODE_API_KEY` in `.env` to a sandbox key (`ck_test_...`). The "Load synthetic patient" button in the UI then:

1. calls `POST /api/finchnode/sessions`, which creates a FinchNode Connect session and immediately calls the sandbox `/simulate` endpoint (scenario from `FINCHNODE_SCENARIO`, default `baseline-adult`), so no hosted login is needed;
2. calls `GET /api/finchnode/sessions/{id}/patient`, which waits for the simulation to complete, reads the patient's records, and returns age, sex, and one-line chart facts (conditions, medications, allergies, recent labs);
3. sends those chart facts as `health_record` with each `/api/suggest` request, so Jev can skip questions the chart already answers.

To skip the simulation and load a patient who has already connected (for example through the session's hosted `url`), put their patient ID (`u_...`, the session's `subject`) in the "FinchNode patient ID" box, or set `FINCHNODE_SUBJECT` in `.env` to use it by default. The button then calls `GET /api/finchnode/patient?subject=...`, which reads that patient's records directly. With neither set, it falls back to the simulation above.

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
- `app/transcribe.py` Gemini speech-to-text with speaker roles
- `app/static/index.html` single-page UI
- `data/urinary_bank.json` drafted question bank, red flags, and fallback checklist
- `scripts/smoke_jev.py` live API smoke test
- `tests/` unit and live tests

## Known limitations

- The question bank is AI-drafted and not clinically validated.
- The tool is for training with simulated patients, not for real clinical decisions.
- Jev's medical ranking quality is unvalidated. The scenario table is a smoke test, not a clinical evaluation.
- Real use would require clinician review, evaluation against expert judgment, and privacy/regulatory review such as HIPAA and clinical decision support rules.
