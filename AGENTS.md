# Repository Agent Instructions

This is `joedeleeuw/typesafe-sdk-python`, a private-use fork of `typesafe-ai/typesafe-sdk-python`. Upstream accepts no pull requests, so fork changes stay here.

## What the fork adds

- `Rank` / `RankModel` questions and `RankAnswer` / `RankItem` answers, exported from `typesafe_sdk`, with `SystemOneResponse.ranks`.
- `src/typesafe_sdk/_core/rank.py` holds the pure logic shared by both clients: expanding a Rank into Choice duels asked in both option orders, the duel schedule (all pairs, or a seeded cycle then most-uncertain pairs up to `max_duels`), the Bradley–Terry MM fit, request chunking under the API's token budget, and merging answers and usage.
- `TypeSafeClient.system_one` sends a Rank's requests sequentially; `AsyncTypeSafeClient.system_one` sends each round's requests concurrently.
- Validation lives in `_core/questions.py` (`_validate_rank`).
- The JS fork (`joedeleeuw/typesafe-sdk-js`) implements the same semantics: same seed (FNV-1a over sorted labels joined by `\x00`, mulberry32, Fisher–Yates), same duel ids, same fit, same answer fields. Change both together.

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

Expect conflicts in `__init__.py`, `question_types.py`, `response_types.py`, both client modules, the API-surface snapshot, and the changelog. If upstream regenerates `_schemas/models.py` with new question or answer types, keep `Rank` out of the wire models: it never reaches the API.

## Validation

- `uv run pyrefly check` and `uv run pytest -m "not integration"` must pass. Update `tests/__snapshots__` with `--snapshot-update` only for intended public-surface changes.
- Integration and doc examples (`-m integration`) need `TYPESAFE_API_KEY`; resolve it with `secret-resolve --service typesafe --key api --env TYPESAFE_API_KEY --op-ref op://Sleepernet/TYPESAFE_API_KEY/credential`. Live calls cost money.
- The fork adds no tests of its own; prove Rank changes with a live smoke.
