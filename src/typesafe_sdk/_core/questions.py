"""Question validation before JSON encoding."""

from collections.abc import Mapping, Sequence

from typesafe_sdk._core.errors import TypeSafeError
from typesafe_sdk._core.json_types import JSONContent
from typesafe_sdk._core.question_types import Choice, Noul, Question, Rank, Score

MAX_CHOICE_OPTIONS = 255
"""The API's limit on options per Choice question, which also bounds the items a Rank can order."""


def normalize_questions(questions: Mapping[str, Question]) -> dict[str, Question]:
    if not questions:
        raise TypeSafeError("At least one question is required.")
    for name, question in questions.items():
        if isinstance(question, Choice):
            _validate_choice_criteria(name, question.criteria)
        elif isinstance(question, Noul):
            _validate_noul(name, question.instructions, question.criteria)
        elif isinstance(question, Rank):
            _validate_rank(name, question.instructions, question.criteria, question.max_duels)
        elif not isinstance(question, Score):
            if not isinstance(question, dict) or not isinstance(question.get("type"), str) or not question["type"]:
                raise TypeSafeError(f'Question "{name}" must be a question object or a dictionary with a nonempty string "type".')
            if question["type"] in ("choice", "score", "rank") and "criteria" not in question:
                raise TypeSafeError(f'Question "{name}" requires "criteria".')
            if question["type"] == "score":
                _validate_score_criteria(name, question["criteria"])
            elif question["type"] == "choice":
                _validate_choice_criteria(name, question["criteria"])
            elif question["type"] == "noul":
                _validate_noul(name, question.get("instructions"), question.get("criteria"))
            elif question["type"] == "rank":
                _validate_rank(name, question.get("instructions"), question["criteria"], question.get("max_duels"))
    return dict(questions)


def is_rank(question: Question) -> bool:
    """Whether a question is a Rank, given as an object or a dictionary."""
    return isinstance(question, Rank) or (isinstance(question, dict) and question.get("type") == "rank")


def _validate_score_criteria(name: str, criteria: Sequence[JSONContent]) -> None:
    """Reject a score question with no criteria; at least one score is required."""
    if not criteria:
        raise TypeSafeError(f'Score question "{name}" has no criteria; at least one score is required.')


def _validate_choice_criteria(name: str, criteria: Mapping[str, JSONContent | None]) -> None:
    """Reject a choice question with no criteria; at least one choice is required."""
    if not criteria:
        raise TypeSafeError(f'Choice question "{name}" has no criteria; at least one choice is required.')


def _validate_noul(name: str, instructions: object, criteria: object) -> None:
    """Reject a noul question with non-object criteria or with neither instructions nor a described outcome."""
    if criteria is not None and not isinstance(criteria, Mapping):
        raise TypeSafeError(f'Noul question "{name}" criteria must be an object.')
    if instructions is None and not (isinstance(criteria, Mapping) and any(value is not None for value in criteria.values())):
        raise TypeSafeError(f'Noul question "{name}" has neither instructions nor criteria; at least one is required.')


def _validate_rank(name: str, instructions: object, criteria: object, max_duels: object) -> None:
    """Reject a rank question without instructions, with too few or too many items, or with an unusable duel budget."""
    if instructions is None:
        raise TypeSafeError(f'Rank question "{name}" requires instructions.')
    if not isinstance(criteria, Mapping):
        raise TypeSafeError(f'Rank question "{name}" criteria must be an object.')
    items = len(criteria)
    if not 2 <= items <= MAX_CHOICE_OPTIONS:
        raise TypeSafeError(f'Rank question "{name}" has {items} criteria; between 2 and {MAX_CHOICE_OPTIONS} items are required.')
    if max_duels is not None and (not isinstance(max_duels, int) or isinstance(max_duels, bool) or max_duels < items - 1):
        raise TypeSafeError(
            f'Rank question "{name}" max_duels must be None or an integer of at least {items - 1}, one fewer than its {items} items.'
        )
