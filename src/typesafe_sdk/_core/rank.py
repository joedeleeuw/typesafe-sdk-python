"""Rank questions: expansion into Choice duels, duel scheduling, Bradley–Terry fitting, and answer merging.

Pure logic shared by the sync and async clients; the clients only send the requests a `RankRun` asks for.
A Rank never reaches the wire. Each scheduled pair of items becomes two Choice questions, one per option
order, sent over the caller's `state` together with the caller's other questions. The soft outcome of a pair
is the mean probability that its first item (in input order) wins across both orders, and Bradley–Terry
strengths are fitted to those outcomes with the MM algorithm (Hunter 2004).

The JavaScript SDK fork implements the same schedule and fit operation for operation, so both produce
bit-identical rankings from the same duel answers. Every sum is an explicit left-to-right loop: Python's
`sum()` uses compensated summation for floats, which JavaScript does not.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx2

from typesafe_sdk._core.errors import TypeSafeError, TypeSafeRankIncompleteError, TypeSafeRankSeparationError
from typesafe_sdk._core.json import deserialize, serialize
from typesafe_sdk._core.json_types import JSONContent
from typesafe_sdk._core.question_types import Choice, Question, Rank
from typesafe_sdk._core.questions import is_rank, normalize_questions
from typesafe_sdk._core.response_types import ChoiceAnswer, RankAnswer, RankDuelOutcome, RankItem, SystemOneResponse, Usage
from typesafe_sdk._core.schemas.base import ResponseT, parse_response

REQUEST_LIMIT_BYTES = 64_000
"""The API's 64k-token limit for the state plus every question of one request, applied to bytes."""
QUESTION_LIMIT_BYTES = 32_000
"""The API's 32k-token limit for the state plus the longest question of one request, applied to bytes.

Sizes are the UTF-8 byte length of the compact JSON encoding. That bounds the token count from above:
every token of a byte-level tokenizer covers at least one byte. Live CLERC requests, prompt template
included, used at least 2.2 bytes per token."""
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
    """The first `budget` edges of a cycle over the items, in a shuffled order seeded from the sorted labels.

    The cycle keeps the duel graph connected and the schedule independent of input order. A budget of one
    fewer than the item count drops the closing edge, leaving a path.
    """
    n = len(labels)
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

    `outcomes` maps each compared pair, in the order played, to the probability that its first item wins.
    Raises when the iteration does not reach a relative tolerance of `FIT_TOLERANCE` within `FIT_MAX_ITERATIONS`.
    """
    wins = [0.0] * n
    for (i, j), outcome in outcomes.items():
        wins[i] = wins[i] + outcome
        wins[j] = wins[j] + (1 - outcome)
    strengths = [1.0] * n
    for _ in range(FIT_MAX_ITERATIONS):
        denominators = [0.0] * n
        for i, j in outcomes:
            inverse = 1 / (strengths[i] + strengths[j])
            denominators[i] = denominators[i] + inverse
            denominators[j] = denominators[j] + inverse
        updated = [wins[i] / denominators[i] for i in range(n)]
        total = 0.0
        for strength in updated:
            total = total + strength
        scale = n / total
        updated = [strength * scale for strength in updated]
        converged = max(abs(updated[i] - strengths[i]) for i in range(n)) <= FIT_TOLERANCE * max(updated)
        strengths = updated
        if converged:
            return strengths
    raise TypeSafeError(f"The Bradley–Terry fit did not converge within {FIT_MAX_ITERATIONS} iterations.")


def _reached_from_first(edges: Sequence[Sequence[int]]) -> list[bool]:
    """Which items the first item reaches along `edges`, where `edges[i]` lists the items `i` points to."""
    reached = [False] * len(edges)
    reached[0] = True
    frontier = [0]
    while frontier:
        for target in edges[frontier.pop()]:
            if not reached[target]:
                reached[target] = True
                frontier.append(target)
    return reached


def separated_items(n: int, outcomes: Mapping[Pair, float]) -> list[int]:
    """A group of items that lost every duel against the items outside it with certainty, in input order.

    An item beats another when it won their duel with positive probability. A finite positive Bradley–Terry
    fit exists exactly when every item beats its way to every other item (Ford 1957, Hunter 2004). Otherwise
    either the first item and the items it beats its way to, or the items that cannot beat their way to the
    first item, never beat any item outside their group. Empty when the fit exists.
    """
    beats: list[list[int]] = [[] for _ in range(n)]
    beaten_by: list[list[int]] = [[] for _ in range(n)]
    for (i, j), outcome in outcomes.items():
        if outcome > 0:
            beats[i].append(j)
            beaten_by[j].append(i)
        if outcome < 1:
            beats[j].append(i)
            beaten_by[i].append(j)
    beaten_from_first = _reached_from_first(beats)
    if not all(beaten_from_first):
        return [k for k in range(n) if beaten_from_first[k]]
    beating_first = _reached_from_first(beaten_by)
    return [k for k in range(n) if not beating_first[k]]


def next_round(n: int, outcomes: Mapping[Pair, float], budget: int) -> list[Pair]:
    """Up to `n` unplayed pairs to ask next, within the remaining budget.

    When a group of items lost every duel against the rest with certainty (`separated_items`), no finite
    positive Bradley–Terry fit exists yet, so the fit is not consumed: the round plays the unplayed pairs
    with exactly one item in that group, in ascending (i, j) order, since those are the only duels that can
    make the fit exist. With no such pair left, the round is empty and the answer raises the separation
    error. Otherwise the round plays the unplayed pairs whose outcome the current fit is least sure of.
    """
    limit = min(n, budget - len(outcomes))
    separated = separated_items(n, outcomes)
    if separated:
        inside = set(separated)
        crossing = [(i, j) for i in range(n) for j in range(i + 1, n) if (i, j) not in outcomes and (i in inside) != (j in inside)]
        return crossing[:limit]
    strengths = fit(n, outcomes)

    def uncertainty(pair: Pair) -> float:
        total = strengths[pair[0]] + strengths[pair[1]]
        p = strengths[pair[0]] / total if total > 0 else 0.5
        return p * (1 - p)

    unplayed = [(i, j) for i in range(n) for j in range(i + 1, n) if (i, j) not in outcomes]
    return sorted(unplayed, key=uncertainty, reverse=True)[:limit]


def _first_wins(answer: ChoiceAnswer, first: str, second: str) -> float:
    """The probability that `first` beats `second`, normalized over the duel's two options."""
    p_first, p_second = answer.probabilities[first], answer.probabilities[second]
    return p_first / (p_first + p_second)


class _RankPlan:
    """The schedule and recorded outcomes of one Rank question."""

    def __init__(self, name: str, rank: Rank) -> None:
        self.name = name
        self.instructions = rank.instructions
        self.criteria = rank.criteria
        self.labels = list(rank.criteria)
        n = len(self.labels)
        self.explicit = rank.max_duels is not None
        self.budget = n * (n - 1) // 2 if rank.max_duels is None else rank.max_duels
        self.outcomes: dict[Pair, float] = {}
        self.consistency: dict[Pair, float] = {}

    def schedule(self) -> list[Pair]:
        n = len(self.labels)
        if not self.outcomes:
            if self.explicit:
                return first_round(self.labels, self.budget)
            return [(i, j) for i in range(n) for j in range(i + 1, n)]
        if len(self.outcomes) >= self.budget:
            return []
        return next_round(n, self.outcomes, self.budget)

    def duel(self, first: int, second: int) -> Choice:
        a, b = self.labels[first], self.labels[second]
        criteria: dict[str, JSONContent | None] = {a: self.criteria[a], b: self.criteria[b]}
        return Choice(instructions=self.instructions, criteria=criteria)

    def largest_duel(self) -> Choice:
        """The duel with the most bytes: JSON object sizes add up, so it pairs the two largest entries."""
        sizes = [len(serialize({label: self.criteria[label]})) for label in self.labels]
        first, second = sorted(range(len(self.labels)), key=sizes.__getitem__, reverse=True)[:2]
        return self.duel(min(first, second), max(first, second))

    def record(self, pair: Pair, a_first: ChoiceAnswer, b_first: ChoiceAnswer) -> None:
        a, b = self.labels[pair[0]], self.labels[pair[1]]
        p_a_first, p_b_first = _first_wins(a_first, a, b), _first_wins(b_first, a, b)
        self.outcomes[pair] = (p_a_first + p_b_first) / 2
        self.consistency[pair] = 1 - abs(p_a_first - p_b_first)

    def answer(self) -> RankAnswer:
        separated = separated_items(len(self.labels), self.outcomes)
        if separated:
            raise TypeSafeRankSeparationError(self.name, tuple(self.labels[k] for k in separated))
        strengths = fit(len(self.labels), self.outcomes)
        best = max(strengths)
        scores = [strength / best for strength in strengths]
        order = sorted(range(len(self.labels)), key=scores.__getitem__, reverse=True)
        consistency = 0.0
        for value in self.consistency.values():
            consistency = consistency + value
        return RankAnswer(
            ranking=tuple(RankItem(label=self.labels[index], score=scores[index]) for index in order),
            duels=len(self.outcomes),
            position_consistency=consistency / len(self.consistency),
            duel_outcomes=tuple(
                RankDuelOutcome(
                    first=self.labels[first],
                    second=self.labels[second],
                    first_wins=outcome,
                    position_consistency=self.consistency[(first, second)],
                )
                for (first, second), outcome in self.outcomes.items()
            ),
        )


def _size(value: object) -> int:
    """UTF-8 bytes of the compact JSON encoding, an upper bound on the value's tokens."""
    return len(serialize(value))


class RankRun:
    """Drives one `system_one` call that contains Rank questions, round by round.

    Clients loop: send every request in `requests()`, pass the responses to `record()`, and stop when
    `requests()` is empty; `result()` then builds the merged response. When a request fails, `incomplete()`
    builds the error that carries the usage of every request that completed.
    """

    def __init__(self, state: JSONContent, questions: Mapping[str, Question]) -> None:
        self._state = state
        self._questions = normalize_questions(questions)
        self._plans = {
            name: _RankPlan(name, question if isinstance(question, Rank) else Rank.model_validate(question))
            for name, question in self._questions.items()
            if is_rank(question)
        }
        self._others = {name: question for name, question in self._questions.items() if name not in self._plans}
        self._state_size = _size(state)
        for name, question in self._others.items():
            size = self._state_size + _size(question)
            if size > QUESTION_LIMIT_BYTES:
                raise TypeSafeError(
                    f'Question "{name}" takes {size} bytes with the state, over the {QUESTION_LIMIT_BYTES}-byte limit for the state and one question.'
                )
        for name, plan in self._plans.items():
            size = self._state_size + _size(plan.largest_duel())
            if size > QUESTION_LIMIT_BYTES:
                raise TypeSafeError(
                    f'Rank question "{name}" has a duel that takes {size} bytes with the state, '
                    f"over the {QUESTION_LIMIT_BYTES}-byte limit for the state and one question."
                )
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
        return self._pack(batch)

    def _pack(self, questions: Mapping[str, Question]) -> list[dict[str, Question]]:
        """Pack questions in order into as few requests as `REQUEST_LIMIT_BYTES` allows.

        Every question already fits `QUESTION_LIMIT_BYTES` with the state, so every request holds at least one.
        """
        chunks: list[dict[str, Question]] = []
        current: dict[str, Question] = {}
        used = self._state_size
        for question_id, question in questions.items():
            size = _size(question)
            if current and used + size > REQUEST_LIMIT_BYTES:
                chunks.append(current)
                current = {}
                used = self._state_size
            current[question_id] = question
            used += size
        if current:
            chunks.append(current)
        return chunks

    def _add_usage(self, responses: Sequence[SystemOneResponse]) -> None:
        for response in responses:
            if self._model is None:
                self._model = response.model
            self._input_tokens += response.usage.input_tokens or 0
            self._output_tokens += response.usage.output_tokens or 0

    def record(self, responses: Sequence[SystemOneResponse]) -> None:
        """Absorb the responses to every request of the current round."""
        self._add_usage(responses)
        choices: dict[str, ChoiceAnswer] = {}
        for response in responses:
            choices.update(response.choices)
            raw_answers = deserialize(response.raw_http_response.content)["answers"]
            self._answers.update({name: raw_answers[name] for name in self._others.keys() & raw_answers.keys()})
        for duel_id, (name, pair) in self._pending.items():
            if duel_id + "ab" not in choices or duel_id + "ba" not in choices:
                raise TypeSafeError(f'The response is missing a duel answer for Rank question "{name}".')
            self._plans[name].record(pair, choices[duel_id + "ab"], choices[duel_id + "ba"])

    def incomplete(self, completed: Sequence[SystemOneResponse]) -> TypeSafeRankIncompleteError:
        """The error for a failed request, carrying the usage of earlier rounds and this round's `completed` requests."""
        self._add_usage(completed)
        return TypeSafeRankIncompleteError(Usage(input_tokens=self._input_tokens, output_tokens=self._output_tokens))

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
