from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeClient:
    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[dict[str, Any]] = []

    async def system_one(self, state, questions, **kwargs):
        call = {
            "state": state,
            "questions": questions,
            "question_keys": list(questions.keys()),
        }
        self.calls.append(call)

        nouls: dict[str, Any] = {}
        choices: dict[str, Any] = {}
        scores: dict[str, Any] = {}
        merged: dict[str, Any] = {}
        for key, question in questions.items():
            answer = self.answers.get(key)
            if answer is None:
                answer = self._default_answer(question)
            answer = self._coerce_answer(question, answer)
            merged[key] = answer
            answer_type = getattr(answer, "type", None)
            if answer_type == "noul":
                nouls[key] = answer
            elif answer_type == "choice":
                choices[key] = answer
            elif answer_type == "score":
                scores[key] = answer

        return SimpleNamespace(answers=merged, nouls=nouls, choices=choices, scores=scores)

    def _default_answer(self, question: Any) -> Any:
        kind = question.__class__.__name__.lower()
        if kind == "noul":
            return SimpleNamespace(type="noul", noul=0.0)
        if kind == "choice":
            criteria = getattr(question, "criteria", {}) or {}
            options = list(criteria.keys())
            choice = options[0] if options else ""
            probabilities = {option: (1.0 if option == choice else 0.0) for option in options}
            return SimpleNamespace(type="choice", choice=choice, confidence=0.0, probabilities=probabilities)
        if kind == "score":
            criteria = list(getattr(question, "criteria", []) or [])
            legend = {index: criteria[index] for index in range(len(criteria))}
            probabilities = {index: (1.0 if index == 0 else 0.0) for index in range(len(criteria))}
            return SimpleNamespace(type="score", score=0.0, confidence=0.0, legend=legend, probabilities=probabilities)
        return SimpleNamespace(type="noul", noul=0.0)

    def _coerce_answer(self, question: Any, answer: Any) -> Any:
        if isinstance(answer, (int, float)):
            kind = question.__class__.__name__.lower()
            if kind == "score":
                criteria = list(getattr(question, "criteria", []) or [])
                legend = {index: criteria[index] for index in range(len(criteria))}
                return SimpleNamespace(type="score", score=float(answer), confidence=0.0, legend=legend, probabilities={index: 0.0 for index in range(len(criteria))})
            return SimpleNamespace(type="noul", noul=float(answer))
        if isinstance(answer, str):
            kind = question.__class__.__name__.lower()
            if kind == "choice":
                criteria = getattr(question, "criteria", {}) or {}
                probabilities = {option: (1.0 if option == answer else 0.0) for option in criteria.keys()}
                return SimpleNamespace(type="choice", choice=answer, confidence=1.0, probabilities=probabilities)
            return SimpleNamespace(type="noul", noul=0.0)
        if isinstance(answer, dict):
            return SimpleNamespace(**answer)
        return answer
