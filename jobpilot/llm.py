"""Thin wrapper around the Anthropic SDK.

Everything that talks to Claude goes through `LLM.json()` or `LLM.text()`, so
tests can swap in a fake with the same two methods. Prompts and the user's
resume are sent to the Anthropic API; nothing else (no keys, no passwords)
ever leaves the machine through this module.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import anthropic

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMBackend(Protocol):
    def json(self, system: str, user: str, schema: dict, *, effort: str | None = None) -> dict: ...
    def text(self, system: str, user: str, *, effort: str | None = None) -> str: ...


def _extract_json(text: str) -> dict:
    """Parse JSON from a model reply, tolerating stray prose or ``` fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise LLMError("The model did not return JSON.")


class LLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if not self.settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set. Add it to your .env file and restart JobPilot.")
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def _run(self, system: str, user: str, effort: str, output_config_extra: dict | None = None) -> str:
        output_config: dict[str, Any] = {"effort": effort}
        if output_config_extra:
            output_config.update(output_config_extra)
        kwargs: dict[str, Any] = dict(
            model=self.settings.anthropic_model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        try:
            if self.settings.llm_fallbacks:
                # Server-side fallback: if a safety classifier declines, the API retries on
                # another model instead of failing. Set LLM_FALLBACKS= (empty) to disable.
                with self.client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks=self.settings.llm_fallbacks,
                    **kwargs,
                ) as stream:
                    message = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit reached. Wait a minute and try again.") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error ({e.status_code}). Try again shortly.") from e
        except anthropic.APIConnectionError as e:
            chain, cur = "", e
            while cur is not None and len(chain) < 2000:
                chain += f" {type(cur).__name__}: {cur}"
                cur = cur.__cause__ or cur.__context__
            if "CERTIFICATE_VERIFY_FAILED" in chain or "SSLCertVerificationError" in chain:
                raise LLMError("Couldn't verify api.anthropic.com's certificate. Antivirus software (such as "
                               "Norton) or a proxy is probably inspecting HTTPS. Run `pip install truststore` in "
                               "JobPilot's environment and restart.") from e
            raise LLMError("Could not reach the Anthropic API. Check your internet connection, and whether a "
                           "firewall or antivirus is blocking Python.") from e

        if message.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if message.stop_reason == "max_tokens":
            raise LLMError("The model's reply was cut off (too long). Try a shorter input.")
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        log.info("LLM call ok: model=%s in=%s out=%s", message.model,
                 message.usage.input_tokens, message.usage.output_tokens)
        return text

    def json(self, system: str, user: str, schema: dict, *, effort: str | None = None) -> dict:
        text = self._run(
            system, user, effort or self.settings.effort_extract,
            {"format": {"type": "json_schema", "schema": schema}},
        )
        return _extract_json(text)

    def text(self, system: str, user: str, *, effort: str | None = None) -> str:
        return self._run(system, user, effort or self.settings.effort_tailor).strip()
