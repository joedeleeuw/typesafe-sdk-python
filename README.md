# TypeSafe AI Python SDK

Python SDK for [TypeSafe AI](https://typesafe.ai).

## Quickstart

Install the SDK:

```
uv add typesafe-sdk
```

Add the `http2` extra (`typesafe-sdk[http2]`) to enable [HTTP/2 support](https://docs.typesafe.ai/sdk/python/usage/#http2).

Set `TYPESAFE_API_KEY` in your environment, then instantiate and use the client:

```python
from typesafe_sdk import Choice, TypeSafeClient

with TypeSafeClient() as client:
    response = client.system_one(
        state={"document": "I was charged twice. Please fix this ASAP."},
        questions={
            "category": Choice(
                instructions="What is this ticket about?",
                criteria={"billing": None, "technical": None, "other": None},
            ),
        },
    )

print(response.choices["category"].choice)
```

learn what TypeSafe is, what it can do, and how to use it in [TypeSafe docs](https://docs.typesafe.ai/).

## Rank (fork addition)

This fork adds a fourth question form, `Rank`, for ordering items when pointwise Score or Noul judgments cluster and cannot pick a clear winner. The client expands a `Rank` into Choice duels between two items, asks each pair in both option orders over the same `state`, and fits Bradley–Terry strengths to the answers:

```python
from typesafe_sdk import Rank, TypeSafeClient

with TypeSafeClient() as client:
    response = client.system_one(
        state={"ticket": "The export button crashes the app, and I was charged twice."},
        questions={
            "triage": Rank(
                instructions="Which issue should support handle first?",
                criteria={"crash": "The app crashes on export", "billing": "A duplicate charge", "typo": "A typo in the footer"},
            ),
        },
    )

answer = response.ranks["triage"]
print([(item.label, round(item.score, 2)) for item in answer.ranking], answer.duels, answer.position_consistency)
```

`ranking` lists every item best first. Each score is the item's strength relative to the winner, which scores exactly 1, so an item with score `s` beats the winner with probability `s / (1 + s)`. `duels` counts the pairs compared, and `position_consistency` is the mean of `1 - |P(a wins | a first) - P(a wins | b first)|` over those pairs, a label-free readout of how much the answers depend on option order. `max_duels=None` (the default) compares every pair; a smaller budget, of at least one fewer than the item count, compares a deterministic cycle first and then the pairs the fit is least sure of. Duels share requests with the call's other questions, usage is summed over every request, and the async client sends each round's requests concurrently.

## Documentation

Learn more in [SDK docs](https://docs.typesafe.ai/sdk/python/).
