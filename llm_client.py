"""
llm_client.py - Thin wrapper around the Google Gemini API (or any OpenAI-compatible
provider) used by the agent loop.

Supports:
  - Google Generative AI (gemini-*)  via google-generativeai SDK
  - OpenAI-compatible endpoints       via openai SDK (set OPENAI_BASE_URL)
  - Anthropic Claude                  via anthropic SDK

The provider is selected by the LLM_PROVIDER environment variable:
  "gemini"    (default) → Google Generative AI
  "openai"              → OpenAI / compatible endpoint
  "anthropic"           → Anthropic Claude
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean_json_response(text: str) -> str:
    """Strip markdown fences that LLMs sometimes add around JSON."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence line
        text = text[text.find("\n") + 1:]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()
    return text.strip()


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _GeminiClient:
    """Wrapper around google.generativeai."""

    def __init__(self, model: str) -> None:
        import google.generativeai as genai  # type: ignore

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY or GOOGLE_API_KEY environment variable is required."
            )
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model,
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 8192,
            },
        )

    def chat(self, messages: list[dict]) -> str:
        """Send a list of {role, content} messages and return the assistant reply."""
        import google.generativeai as genai  # type: ignore

        history = []
        system_text = None

        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                system_text = content
            elif role == "user":
                history.append({"role": "user", "parts": [content]})
            elif role == "assistant":
                history.append({"role": "model", "parts": [content]})

        if system_text:
            # Prepend system text as first user turn (Gemini doesn't have a system role)
            history.insert(0, {"role": "user", "parts": [f"[SYSTEM]\n{system_text}"]})
            history.insert(1, {"role": "model", "parts": ["Understood."]})

        response = self._model.generate_content(history)
        return response.text


class _OpenAIClient:
    """Wrapper around the openai SDK (also supports compatible providers)."""

    def __init__(self, model: str) -> None:
        from openai import OpenAI  # type: ignore

        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")  # optional
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY environment variable is required.")
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._model = model

    def chat(self, messages: list[dict]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.1,
            max_tokens=8192,
        )
        return response.choices[0].message.content or ""


class _AnthropicClient:
    """Wrapper around the anthropic SDK."""

    def __init__(self, model: str) -> None:
        import anthropic  # type: ignore

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is required.")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def chat(self, messages: list[dict]) -> str:
        import anthropic  # type: ignore

        system_text = None
        anthropic_msgs: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                anthropic_msgs.append({"role": msg["role"], "content": msg["content"]})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": anthropic_msgs,
        }
        if system_text:
            kwargs["system"] = system_text

        response = self._client.messages.create(**kwargs)
        return response.content[0].text


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_llm_client(provider: str | None = None, model: str | None = None):
    """
    Return an LLM client instance configured from environment variables.

    LLM_PROVIDER : "gemini" | "openai" | "anthropic"   (default: "gemini")
    LLM_MODEL    : model name override
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        default_model = "gemini-2.0-flash"
        return _GeminiClient(model or os.environ.get("LLM_MODEL", default_model))
    elif provider == "openai":
        default_model = "gpt-4o-mini"
        return _OpenAIClient(model or os.environ.get("LLM_MODEL", default_model))
    elif provider == "anthropic":
        default_model = "claude-3-5-haiku-20241022"
        return _AnthropicClient(model or os.environ.get("LLM_MODEL", default_model))
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}. Choose gemini, openai, or anthropic.")


def parse_json_response(text: str) -> Any:
    """Parse JSON from LLM text, stripping markdown fences if present."""
    cleaned = _clean_json_response(text)
    return json.loads(cleaned)
