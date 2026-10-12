"""Asynchronous API client."""

import asyncio
import ssl
import threading
from collections.abc import Mapping
from functools import cached_property
from types import TracebackType
from typing import Any, overload

import httpx2
from packaging.version import Version
from typing_extensions import Self

from typesafe_sdk._core.client.aio.models import AsyncModels
from typesafe_sdk._core.config import DEFAULT_LIMITS, Config
from typesafe_sdk._core.endpoints import prepare_system_one
from typesafe_sdk._core.errors import TypeSafeError
from typesafe_sdk._core.json_types import JSONContent, JSONValue
from typesafe_sdk._core.question_types import Question
from typesafe_sdk._core.questions import is_rank
from typesafe_sdk._core.rank import RankRun
from typesafe_sdk._core.response_types import SystemOneResponse
from typesafe_sdk._core.retry import RetryPolicy, build_tenacity_async
from typesafe_sdk._core.transport import Request, ResponseT, send_async


def _ssl_context() -> ssl.SSLContext:
    """httpx2's default SSL context, with `wrap_bio()` serialized on affected truststore versions.

    AnyIO calls `wrap_bio()` from worker threads, and truststore<=0.10.4 reconfigures the shared
    context there without a lock, which can abort the interpreter (sethmlarson/truststore#221).
    """
    ctx = httpx2.create_ssl_context()
    import truststore  # noqa: PLC0415 - Unavailable on emscripten; httpx2 imports it lazily too.

    if not isinstance(ctx, truststore.SSLContext) or Version(truststore.__version__) > Version("0.10.4"):
        return ctx
    wrap_bio = ctx.wrap_bio
    lock = threading.Lock()

    def locked_wrap_bio(*args: Any, **kwargs: Any) -> ssl.SSLObject:
        with lock:
            return wrap_bio(*args, **kwargs)

    ctx.wrap_bio = locked_wrap_bio
    return ctx


class AsyncTypeSafeClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | httpx2.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        """Create an asynchronous HTTP client for [TypeSafe AI API](https://typesafe.ai).

        Explicit options take precedence over environment variables; empty or whitespace-only
        environment values are ignored.

        !!! tip "Logging setup"
            The SDK logs to the `typesafe_sdk` logger; configure it through standard logging, or set
            `TYPESAFE_LOG_LEVEL` (`debug`, `info`, ...) for a quick default. Secret headers are
            redacted from log output; request and response bodies are not.

        Args:
            api_key: Required API key; may be set via the `TYPESAFE_API_KEY` environment variable.
                Leading and trailing whitespace is stripped. Empty keys, internal whitespace,
                control characters, and non-ASCII characters are rejected.
            model: Model name; may be set via the `TYPESAFE_DEFAULT_MODEL` environment variable.
            retry: A `RetryPolicy` controlling retry behavior; see `RetryPolicy` for the available options and their
                defaults. Pass `RetryPolicy(max_retries=0)` to disable retries.
            timeout: Timeout for HTTP operations. Inherits `http_client.timeout` when supplied, otherwise the SDK default.
            headers: Additional request headers to set.
            transport: Optional custom HTTP transport, closed when this SDK client closes.
            http_client: Optional `httpx2.AsyncClient`; mutually exclusive with `transport`.
                Closed when this SDK client closes.
            base_url: API root; may be set via the `TYPESAFE_BASE_URL` environment variable.

        Raises:
            TypeSafeError: The API key is missing or invalid, or the timeout is invalid.
            ValueError: Both `transport` and `http_client` are supplied.

        Examples:
            ```python
            import asyncio

            from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul


            async def main() -> None:
                async with AsyncTypeSafeClient() as client:
                    result = await client.system_one(
                        state="I was charged twice. Please help.",
                        questions={
                            "billing": Noul(instructions="Is this about billing?"),
                            "tone": Choice(
                                instructions="What is the tone?",
                                criteria={"calm": None, "angry": None},
                            ),
                        },
                    )
                    assert 0 <= result.nouls["billing"].noul <= 1
                    assert result.choices["tone"].choice in {"calm", "angry"}


            asyncio.run(main())
            ```
        """
        if transport is not None and http_client is not None:
            raise ValueError("transport and http_client are mutually exclusive.")
        if timeout is None and http_client is not None:
            timeout = http_client.timeout
        self._config = Config.resolve(api_key, base_url, model, timeout, headers)
        self._retry = build_tenacity_async(retry)
        self._http_client = (
            httpx2.AsyncClient(timeout=self._config.timeout, limits=DEFAULT_LIMITS, transport=transport, verify=_ssl_context())
            if http_client is None
            else http_client
        )

    @cached_property
    def models(self) -> AsyncModels:
        """An accessor for the Models API resource.

        Examples:
            ```python
            async def main() -> None:
                async with AsyncTypeSafeClient() as client:
                    models = await client.models.list()
            ```
        """
        return AsyncModels(self._config, self._http_client, self._retry)

    @overload
    async def system_one(
        self,
        state: JSONContent,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | httpx2.Timeout | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, JSONValue | None] | None = None,
        response_model: None = None,
    ) -> SystemOneResponse: ...

    @overload
    async def system_one(
        self,
        state: JSONContent,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | httpx2.Timeout | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, JSONValue | None] | None = None,
        response_model: type[ResponseT],
    ) -> ResponseT: ...

    async def system_one(
        self,
        state: JSONContent,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | httpx2.Timeout | None = None,
        extra_headers: Mapping[str, str] | None = None,
        extra_body: Mapping[str, JSONValue | None] | None = None,
        response_model: type[ResponseT] | None = None,
    ) -> SystemOneResponse | ResponseT:
        """Answer named questions about text or structured state.

        See [System One](https://docs.typesafe.ai/concepts/system-one) for details.

        Args:
            state: Text, a JSON object, or an array to evaluate.
                See [state](https://docs.typesafe.ai/concepts/state) for details.
            questions: Nonempty mapping of names to question objects or raw dictionaries. A `Rank` among them
                is expanded into Choice duels, sent concurrently in as few requests per round as the API's request
                size limits allow; the returned response then sums usage over every request and has no
                `raw_http_response` or `request_id`.
            model: Model override; `None` inherits the client default.
            retry: An optional retry policy to override the client-level value for this call only.
            timeout: An optional timeout for http operations to override the client-level value for this call only, in seconds.
            extra_headers: Additional request headers to set.
            extra_body: Additional top-level request-body fields, shallow-merged over the body after
                `state`, `model`, and `questions` are set. Merging is last-write-wins: a key that
                collides with `state`, `model`, or `questions` overrides it, and object values are
                replaced rather than deep-merged.
            response_model: Optional Pydantic `BaseModel` type describing the
                JSON response body, including any nested answer models.

        Returns:
            An instance of `response_model`, or `SystemOneResponse` with answers keyed by question
            name and model and token usage details when no custom model is supplied.

        Raises:
            TypeSafeError: State is `None`, questions are empty, a score or choice question has no criteria,
                a noul question has neither instructions nor criteria, a rank question has no instructions,
                fewer than 2 or more than 255 items, an integer-like label, or a `max_duels` outside one fewer
                than its item count to its pair count, a question in a call with a Rank takes more than 32,000
                bytes of JSON together with the state, or a Rank's Bradley–Terry fit does not converge.
            TypeSafeRankIncompleteError: A request of a call with a Rank fails; the error carries the usage of
                every request that completed and has the failure as its `__cause__`.
            TypeSafeRankSeparationError: A group of a Rank's items lost every duel against the other items with
                certainty, so the Bradley–Terry fit has no finite positive strengths; raised before any score.
            TypeSafeAPIError: The server returns an unsuccessful HTTP response after any retries.
            TypeSafeAPIConnectionError: The request cannot connect or times out after any retries.
            TypeSafeAPIResponseValidationError: The response body does not match the response model.

        Examples:
            Create questions with named arguments:

            ```python
            async def main() -> None:
                async with AsyncTypeSafeClient() as client:
                    result = await client.system_one(
                        state="I was charged twice. Please help.",
                        questions={
                            "billing": Noul(instructions="Is this about billing?"),
                            "tone": Choice(
                                instructions="What is the tone?",
                                criteria={"calm": None, "angry": None},
                            ),
                        },
                    )
                    assert 0 <= result.nouls["billing"].noul <= 1
                    assert result.choices["tone"].choice in {"calm", "angry"}
            ```

            Pass questions as dictionaries:

            ```python
            async def main() -> None:
                async with AsyncTypeSafeClient() as client:
                    result = await client.system_one(
                        state={"message": "I was charged twice. Please help."},
                        questions={
                            "billing": {"type": "noul", "instructions": "Is this about billing?"},
                            "tone": {
                                "type": "choice",
                                "instructions": "What is the tone?",
                                "criteria": {"calm": None, "angry": None},
                            },
                        },
                    )
                    assert 0 <= result.nouls["billing"].noul <= 1
                    assert result.choices["tone"].choice in {"calm", "angry"}
            ```
        """
        if any(is_rank(question) for question in questions.values()):
            run = RankRun(state, questions)
            while chunks := run.requests():
                # Let every request of the round settle, so a failure still accounts for its siblings' usage.
                settled = await asyncio.gather(
                    *(
                        self._request(
                            prepare_system_one(self._config, state, chunk, model, extra_body, timeout, extra_headers, SystemOneResponse),
                            retry=retry,
                        )
                        for chunk in chunks
                    ),
                    return_exceptions=True,
                )
                responses = [outcome for outcome in settled if isinstance(outcome, SystemOneResponse)]
                failures = [outcome for outcome in settled if isinstance(outcome, BaseException)]
                if failures:
                    if not isinstance(failures[0], TypeSafeError):
                        raise failures[0]
                    raise run.incomplete(responses) from failures[0]
                run.record(responses)
            return run.result(SystemOneResponse if response_model is None else response_model)
        return await self._request(
            prepare_system_one(
                self._config,
                state,
                questions,
                model,
                extra_body,
                timeout,
                extra_headers,
                SystemOneResponse if response_model is None else response_model,
            ),
            retry=retry,
        )

    async def _request(self, request: Request[ResponseT], *, retry: RetryPolicy | None = None) -> ResponseT:
        return await send_async(self._http_client, self._retry, request, retry)

    async def aclose(self) -> None:
        """Release network resources and close the underlying HTTP client, including a supplied one."""
        await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        """Enter the asynchronous client context."""
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        """Close the asynchronous client context."""
        await self.aclose()
