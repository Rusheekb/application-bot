"""Thin wrapper around the Anthropic SDK shared by fit scoring, resume parsing, writing and the
auto-apply agent."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from jobbot.config import anthropic_credentials_present
from jobbot.schemas import AISettings

log = logging.getLogger(__name__)

# Server-side refusal fallback: if a safety classifier declines a request, the API re-runs it on
# the model Anthropic recommends for that refusal category instead of returning the refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

MODEL_CHOICES = {
    "claude-opus-5-5": "Claude Opus 5.5 (default, most capable everyday model)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (faster, about half the price)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest; fine for bulk scoring)",
    "claude-fable-5-1": "Claude Fable 5.1 (most capable, most expensive)",
}

# USD per million tokens: (input, output, cache read, 5-minute cache write).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
}


class AIUnavailable(Exception):
    """No Anthropic credentials are configured, or AI features are switched off."""


class AIRefused(Exception):
    """The model (or a safety classifier) declined the request."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    models: set[str] = field(default_factory=set)

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        if model := getattr(response, "model", None):
            self.models.add(model)

    def cost_usd(self, model: str) -> float | None:
        price = PRICES.get(model)
        if price is None:
            return None
        p_in, p_out, p_read, p_write = price
        return (
            self.input_tokens * p_in
            + self.output_tokens * p_out
            + self.cache_read_tokens * p_read
            + self.cache_write_tokens * p_write
        ) / 1_000_000

    def as_dict(self, model: str) -> dict[str, Any]:
        cost = self.cost_usd(model)
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(cost, 4) if cost is not None else None,
        }


def ai_ready(settings: AISettings) -> bool:
    return settings.enabled and anthropic_credentials_present()


class LLM:
    """Messages API calls with the app's model/effort settings, refusal handling and usage tracking."""

    def __init__(self, settings: AISettings, client: Any | None = None):
        self.settings = settings
        self.model = settings.model
        self.client = client if client is not None else AsyncAnthropic(max_retries=3)
        self.usage = Usage()
        self._fallbacks = settings.fallbacks and self.model in FALLBACK_MODELS

    @classmethod
    def from_settings(cls, settings: AISettings) -> LLM:
        if not settings.enabled:
            raise AIUnavailable("AI features are turned off in Settings")
        if not anthropic_credentials_present():
            raise AIUnavailable("Set ANTHROPIC_API_KEY in your .env file to enable AI features")
        return cls(settings)

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: AISettings) -> AsyncIterator[LLM]:
        """`async with LLM.open(settings) as llm:`, closing the HTTP client afterwards."""
        llm = cls.from_settings(settings)
        try:
            yield llm
        finally:
            await llm.aclose()

    async def aclose(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def create(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        tools: list[dict[str, Any]] | None = None,
        output_format: type[BaseModel] | None = None,
        auto_cache: bool = False,
        thinking: dict[str, Any] | None = None,
    ) -> Any:
        """One Messages API request. Raises AIRefused when the request is declined."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
        }
        if tools:
            kwargs["tools"] = tools
        if auto_cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if thinking:
            kwargs["thinking"] = thinking
        response = await self._send(kwargs, output_format)
        self.usage.add(response)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AIRefused(f"Claude declined this request{f' ({category})' if category else ''}")
        return response

    async def _send(self, kwargs: dict[str, Any], output_format: type[BaseModel] | None) -> Any:
        while True:
            call = dict(kwargs)
            if self._fallbacks:
                call["betas"] = [FALLBACK_BETA]
                call["fallbacks"] = "default"
            try:
                if output_format is not None:
                    return await self.client.beta.messages.parse(output_format=output_format, **call)
                return await self.client.beta.messages.create(**call)
            except anthropic.BadRequestError as exc:
                # Some accounts/platforms don't offer server-side fallbacks; carry on without them.
                if self._fallbacks and "fallback" in str(exc).lower():
                    log.warning("Server-side refusal fallbacks unavailable; continuing without them")
                    self._fallbacks = False
                    continue
                raise


def response_text(response: Any) -> str:
    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()


def describe_error(exc: Exception) -> str:
    """Short, user-facing explanation of an AI failure."""
    if isinstance(exc, (AIUnavailable, AIRefused)):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected the API key (check ANTHROPIC_API_KEY)"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Your Anthropic API key doesn't have access to this model"
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found; pick another model in Settings"
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API; try again in a minute"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API (network error)"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error ({exc.status_code}): {getattr(exc, 'message', exc)}"
    return f"{exc.__class__.__name__}: {exc}"
