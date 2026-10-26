"""Claude-powered features: fit analysis, resume parsing, writing help and the apply agent."""

from jobbot.ai.client import LLM, AIRefused, AIUnavailable, ai_ready, describe_error

__all__ = ["LLM", "AIRefused", "AIUnavailable", "ai_ready", "describe_error"]
