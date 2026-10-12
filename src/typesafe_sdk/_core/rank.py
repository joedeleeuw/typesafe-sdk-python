"""Rank questions: expansion into Choice duels, duel scheduling, Bradley–Terry fitting, and answer merging.

Pure logic shared by the sync and async clients; the clients only send the requests a `RankRun` asks for.
A Rank never reaches the wire. Each scheduled pair of items becomes two Choice questions, one per option
order, sent over the caller's `state` together with the caller's other questions. The soft outcome of a pair
is the mean probability that its first item (in input order) wins across both orders, and Bradley–Terry
strengths are fitted to those outcomes with the MM algorithm (Hunter 2004).
"""

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx2

from typesafe_sdk._core.errors import TypeSafeError
from typesafe_sdk._core.json import deserialize, serialize
from typesafe_sdk._core.json_types import JSONContent
from typesafe_sdk._core.question_types import Choice, Question, Rank
from typesafe_sdk._core.questions import is_rank, normalize_questions
from typesafe_sdk._core.response_types import ChoiceAnswer, RankAnswer, RankItem, SystemOneResponse
from typesafe_sdk._core.schemas.base import ResponseT, parse_response

REQUEST_TOKEN_BUDGET = 60_000
"""Estimated tokens per request, under the API's 64k budget for the state plus every question."""
BYTES_PER_TOKEN = 2
"""A deliberately low ratio of UTF-8 JSON bytes per token, so the estimate errs towards more, smaller requests.
Citation-dense legal text runs as low as about 2.2 bytes per token."""
FIT_TOLERANCE = 1e-10
FIT_MAX_ITERATIONS = 10_000
_UINT32 = 0xFFFFFFFF

Pair = tuple[int, int]
"""Two item indices in input order, the first lower than the second."""


def _fnv1a(text: str) -> int:
    """32-bit FNV-1a over the UTF-8 bytes of `text`."""
    value = 0x811C9DC5
    for byte in text.encode():
        value = ((value ^ byte) * 0x01000193) & _UINT32
    return value


def _mulberry32(seed: int) -> Callable[[], float]:
    """The mulberry32 generator, bit-for-bit identical to its usual JavaScript form."""
    state = seed & _UINT32

    def imul(a: int, b: int) -> int:
        return (a * b) & _UINT32

    def draw() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _UINT32
        t = imul(state ^ (state >> 15), 1 | state)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & _UINT32) ^ t
        return ((t ^ (t >> 14)) & _UINT32) / 2**32

    return draw


def first_round(labels: Sequence[str], budget: int) -> list[Pair]:
    """Pairs for the first round: every pair when the budget covers them all, otherwise a cycle.

    The cycle visits the items in a shuffled order seeded from the sorted labels, which keeps the duel graph
    connected and the schedule independent of input order. A budget of one fewer than the item count drops
    the closing edge, leaving a path.
    """
    n = len(labels)
    if budget >= n * (n - 1) // 2:
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    order = sorted(range(n), key=labels.__getitem__)
    draw = _mulberry32(_fnv1a("\x00".join(sorted(labels))))
    for i in range(n - 1, 0, -1):
        j = math.floor(draw() * (i + 1))
        order[i], order[j] = order[j], order[i]
    cycle: list[Pair] = []
    for k in range(n):
        a, b = order[k], order[(k + 1) % n]
        pair = (min(a, b), max(a, b))
        if pair not in cycle:
            cycle.append(pair)
    return cycle[:budget]


def fit(n: int, outcomes: Mapping[Pair, float]) -> list[float]:
    """Bradley–Terry strengths by Hunter's MM algorithm on soft outcomes, normalized to sum to `n`.

    `outcomes` maps each compared pair to the probability that its first item wins. Raises when the
    iteration does not reach a relative tolerance of `FIT_TOLERANCE` within `FIT_MAX_ITERATIONS`.
    """
    wins = [0.0] * n
    opponents: list[list[int]] = [[] for _ in range(n)]
    for (i, j), outcome in outcomes.items():
        wins[i] += outcome
        wins[j] += 1 - outcome
        opponents[i].append(j)
        opponents[j].append(i)
    strengths = [1.0] * n
    for _ in range(FIT_MAX_ITERATIONS):
        updated = [wins[i] / sum(1 / (strengths[i] + strengths[j]) for j in opponents[i]) for i in range(n)]
        scale = n / sum(updated)
        updated = [strength * scale for strength in updated]
        converged = max(abs(new - old) for new, old in zip(updated, strengths)) <= FIT_TOLERANCE * max(updated)
        strengths = updated
        if converged:
            return strengths
    raise TypeSafeError(f"The Bradley–Terry fit did not converge within {FIT_MAX_ITERATIONS} iterations.")


def next_round(n: int, outcomes: Mapping[Pair, float], budget: int) -> list[Pair]:
    """Up to `n` unplayed pairs whose outcome the current fit is least sure of, within the remaining budget."""
    strengths = fit(n, outcomes)

    def uncertainty(pair: Pair) -> float:
        total = strengths[pair[0]] + strengths[pair[1]]
        p = strengths[pair[0]] / total if total else 0.5
        return p * (1 - p)

    unplayed = [(i, j) for i in range(n) for j in range(i + 1, n) if (i, j) not in outcomes]
    return sorted(unplayed, key=uncertainty, reverse=True)[: min(n, budget - len(outcomes))]


def _first_wins(answer: ChoiceAnswer, first: str, second: str) -> float:
    """The probability that `first` beats `second`, normalized over the duel's two options."""
    p_first, p_second = answer.probabilities[first], answer.probabilities[second]
    return p_first / (p_first + p_second)


class _RankPlan:
    """The schedule and recorded outcomes of one Rank question."""

    def __init__(self, rank: Rank) -> None:
        self.instructions = rank.instructions
        self.criteria = rank.criteria
        self.labels = list(rank.criteria)
        n = len(self.labels)
        pairs = n * (n - 1) // 2
        self.budget = pairs if rank.max_duels is None else min(rank.max_duels, pairs)
        self.outcomes: dict[Pair, float] = {}
        self.consistency: dict[Pair, float] = {}

    def schedule(self) -> list[Pair]:
        if not self.outcomes:
            return first_round(self.labels, self.budget)
        if len(self.outcomes) >= self.budget:
            return []
        return next_round(len(self.labels), self.outcomes, self.budget)

    def duel(self, first: int, second: int) -> Choice:
        a, b = self.labels[first], self.labels[second]
        criteria: dict[str, JSONContent | None] = {a: self.criteria[a], b: self.criteria[b]}
        return Choice(instructions=self.instructions, criteria=criteria)

    def record(self, pair: Pair, a_first: ChoiceAnswer, b_first: ChoiceAnswer) -> None:
        a, b = self.labels[pair[0]], self.labels[pair[1]]
        p_a_first, p_b_first = _first_wins(a_first, a, b), _first_wins(b_first, a, b)
        self.outcomes[pair] = (p_a_first + p_b_first) / 2
        self.consistency[pair] = 1 - abs(p_a_first - p_b_first)

    def answer(self) -> RankAnswer:
        strengths = fit(len(self.labels), self.outcomes)
        best = max(strengths)
        order = sorted(range(len(self.labels)), key=strengths.__getitem__, reverse=True)
        return RankAnswer(
            ranking=tuple(RankItem(label=self.labels[index], score=strengths[index] / best) for index in order),
            duels=len(self.outcomes),
            position_consistency=sum(self.consistency.values()) / len(self.consistency),
        )


def _estimate_tokens(value: object) -> int:
    return math.ceil(len(serialize(value)) / BYTES_PER_TOKEN)


def _chunk(state: JSONContent, questions: Mapping[str, Question]) -> list[dict[str, Question]]:
    """Pack questions in order into as few requests as the estimated token budget allows."""
    base = _estimate_tokens(state)
    chunks: list[dict[str, Question]] = []
    current: dict[str, Question] = {}
    used = base
    for question_id, question in questions.items():
        cost = _estimate_tokens(question)
        if current and used + cost > REQUEST_TOKEN_BUDGET:
            chunks.append(current)
            current = {}
            used = base
        current[question_id] = question
        used += cost
    if current:
        chunks.append(current)
    return chunks


class RankRun:
    """Drives one `system_one` call that contains Rank questions, round by round.

    Clients loop: send every request in `requests()`, pass the responses to `record()`, and stop when
    `requests()` is empty; `result()` then builds the merged response.
    """

    def __init__(self, state: JSONContent, questions: Mapping[str, Question]) -> None:
        self._state = state
        self._questions = normalize_questions(questions)
        self._plans = {
            name: _RankPlan(question if isinstance(question, Rank) else Rank.model_validate(question))
            for name, question in self._questions.items()
            if is_rank(question)
        }
        self._others = {name: question for name, question in self._questions.items() if name not in self._plans}
        colons = 1
        while any(name.startswith("rank" + ":" * colons) for name in self._questions):
            colons += 1
        self._prefix = "rank" + ":" * colons
        self._pending: dict[str, tuple[str, Pair]] = {}
        self._answers: dict[str, Any] = {}
        self._model: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0

    def requests(self) -> list[dict[str, Question]]:
        """The question chunks of the next round, one request each; empty once every Rank is complete."""
        batch: dict[str, Question] = dict(self._others) if self._model is None else {}
        self._pending = {}
        for name, plan in self._plans.items():
            for first, second in plan.schedule():
                duel_id = f"{self._prefix}{name}:{first}:{second}:"
                batch[duel_id + "ab"] = plan.duel(first, second)
                batch[duel_id + "ba"] = plan.duel(second, first)
                self._pending[duel_id] = (name, (first, second))
        return _chunk(self._state, batch) if batch else []

    def record(self, responses: Sequence[SystemOneResponse]) -> None:
        """Absorb the responses to every request of the current round."""
        choices: dict[str, ChoiceAnswer] = {}
        for response in responses:
            if self._model is None:
                self._model = response.model
            self._input_tokens += response.usage.input_tokens or 0
            self._output_tokens += response.usage.output_tokens or 0
            choices.update(response.choices)
            raw_answers = deserialize(response.raw_http_response.content)["answers"]
            self._answers.update({name: raw_answers[name] for name in self._others.keys() & raw_answers.keys()})
        for duel_id, (name, pair) in self._pending.items():
            if duel_id + "ab" not in choices or duel_id + "ba" not in choices:
                raise TypeSafeError(f'The response is missing a duel answer for Rank question "{name}".')
            self._plans[name].record(pair, choices[duel_id + "ab"], choices[duel_id + "ba"])

    def result(self, response_type: type[ResponseT]) -> ResponseT:
        """The merged response: caller answers and Rank answers in question order, usage summed over every request."""
        answers: dict[str, Any] = {}
        for name in self._questions:
            if name in self._plans:
                answers[name] = self._plans[name].answer().model_dump(mode="json")
            elif name in self._answers:
                answers[name] = self._answers[name]
        body = {
            "model": self._model,
            "answers": answers,
            "usage": {"input_tokens": self._input_tokens, "output_tokens": self._output_tokens},
        }
        result = parse_response(httpx2.Response(200, content=serialize(body)), response_type)
        # The merged response spans several HTTP exchanges, so it has no single raw response to expose.
        result.__dict__.pop("_raw", None)
        return result
