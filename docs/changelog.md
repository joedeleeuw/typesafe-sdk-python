---
title: Changelog
icon: lucide/history
---

# Changelog

## Unreleased

### Features

- add `Rank`, a fork-only question form that orders items by pairwise Choice duels asked in both option orders and fitted with Bradley–Terry; answers arrive as `RankAnswer` (`ranking`, `duels`, `position_consistency`) under `SystemOneResponse.ranks`
- add `TypeSafeRankIncompleteError`, raised when a request of a call with a Rank fails, carrying the usage of the requests that completed

## v0.7.4 (2026-10-09)

### Bug fixes

- serialize non-finite numbers as strings instead of invalid JSON tokens

## v0.7.3 (2026-10-09)

### Bug fixes

- prevent async client crashes during concurrent TLS connections with `truststore<=0.10.4`
- add more request validation checks

### Documentation

- add a new Performance section with tips for high-concurrency usage

### Performance

- update the default settings to keep all pooled connections alive for 30 seconds

## v0.7.2 (2026-09-26)

### Miscellaneous

- add `http2` extra to `typesafe-sdk` package

### Documentation

- document `typesafe-sdk` usage with HTTP/2 support

## v0.7.1 (2026-09-21)

### Bug fixes

- validate the API key early and exclude the value from logged exceptions

### Documentation

- add examples for usage with AI gateways

## v0.7.0 (2026-09-18)

### Breaking Changes

- ser/de library has been changed from `msgspec` to `pydantic`

### Bug fixes

- `str` subclasses are now correctly serialized as strings instead of lists of characters

### Features

- the `system_one` method now accepts a new `response_model` argument that can be set to a desired `pydantic` model for additional _type-safety_

## v0.6.0 (2026-09-15)

### Breaking Changes

- accept `Score.criteria` as an ordered sequence instead of a dictionary keyed by integers

### Features

- improve type annotations on SDK inputs to accept abstract types like `Mapping` and `Sequence`
- improve error messages to include http details and metadata

### Bug fixes

- handle invalid values in `RetryPolicy`
- make exceptions and responses picklable

### Documentation

- link more concepts from main [docs](https://docs.typesafe.ai/)

## v0.5.7 (2026-09-14)

This is the initial public release of TypeSafe Python SDK. Learn more in the [documentation](https://docs.typesafe.ai/sdk/python).
