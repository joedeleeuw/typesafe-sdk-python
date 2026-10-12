# Repository Agent Instructions

This is `joedeleeuw/typesafe-sdk-python`, a private-use fork of `typesafe-ai/typesafe-sdk-python`. Upstream accepts no pull requests, so fork changes stay here.

## What the fork adds

- `Rank` / `RankModel` questions, `RankAnswer` / `RankItem` / `RankDuelOutcome` answers, and `TypeSafeRankIncompleteError` / `TypeSafeRankSeparationError`, exported from `typesafe_sdk`, with `SystemOneResponse.ranks`.
- `src/typesafe_sdk/_core/rank.py` holds the pure logic shared by both clients: expanding a Rank into Choice duels asked in both option orders, the duel schedule (all pairs at once for `max_duels=None`; otherwise a seeded cycle, then most-uncertain pairs up to `max_duels`), the Bradley–Terry MM fit, request packing under the API's 64k request and 32k state-plus-question limits (UTF-8 JSON bytes as the token bound), and merging answers and usage.
- `TypeSafeClient.system_one` sends a Rank's requests sequentially; `AsyncTypeSafeClient.system_one` sends each round's requests concurrently. A failed request raises `TypeSafeRankIncompleteError` with the usage of the requests that completed.
- Before fitting the final ranking, `separated_items` checks that every item beats its way to every other item through duels won with positive probability; otherwise the Bradley–Terry MLE has zero strengths and `answer()` raises `TypeSafeRankSeparationError`. Scheduling rounds run the same check first: while a group is separated, a round plays the unplayed pairs crossing into that group (the only duels that can make the fit exist) instead of fitting.
- Validation lives in `_core/questions.py` (`_validate_rank`): instructions required, 2–255 non-integer labels, `max_duels` from n−1 to n(n−1)/2.
- The JS fork (`joedeleeuw/typesafe-sdk-js`) implements the same semantics bit for bit: same seed (FNV-1a over code-point-sorted labels joined by `\x00`, mulberry32, Fisher–Yates), same duel ids, same validation and error texts, and the same fit arithmetic in the same order. Every sum is an explicit left-to-right loop, because Python's `sum()` compensates float rounding and JavaScript does not. Change both together and re-check parity by feeding identical recorded duel probabilities into both and comparing schedules and score bits.

Fork changes are listed under `## Unreleased` in `docs/changelog.md` and in the README's Rank section.

## Syncing upstream releases

Remotes: `origin` is the fork, `upstream` is `typesafe-ai/typesafe-sdk-python`. Upstream publishes squashed `Release vX.Y.Z` commits on `main`.

```bash
git fetch upstream
git rebase upstream/main   # or merge; keep fork commits on top
uv sync
uv run pyrefly check && uv run pytest -m "not integration"
git push --force-with-lease origin main
```

Expect conflicts in `__init__.py`, `question_types.py`, `response_types.py`, both client modules, and the changelog. If upstream regenerates `_schemas/models.py` with new question or answer types, keep `Rank` out of the wire models: it never reaches the API.

## Validation

- `uv run pyrefly check` and `uv run pytest -m "not integration"` must pass.
- The published repo carries no ruff configuration, and its only workflow (`.github/workflows/publish.yml`) builds and publishes without linting. Upstream code is formatted at a 145-character line length; `uv run ruff format --line-length 145 --check src` is clean. Plain `ruff check` reports upstream `noqa` directives for rules its private config enables (RUF100); leave them.
- Integration and doc examples (`-m integration`) need `TYPESAFE_API_KEY`; resolve it with `secret-resolve --service typesafe --key api --env TYPESAFE_API_KEY --op-ref op://Sleepernet/TYPESAFE_API_KEY/credential`. Live calls cost money.
- The fork adds no tests of its own; prove Rank changes with a live smoke.
