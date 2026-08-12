"""OpenAI-compatible transport for the L2 model service.

Configuration comes from the environment and nowhere else — no URL, key or model
name is written down in this repository, as the rules require:

===========================  =========================================
``LOOMQ_LLM_BASE_URL``       API root
``LOOMQ_LLM_API_KEY``        credential for this run
``LOOMQ_LLM_MODEL``          model id (``deepseek-v4-flash`` when scored)
``LOOMQ_LLM_TIMEOUT_SECONDS``  per-request timeout, default 120
===========================  =========================================

Request parameters follow ``l2_policy.json``: non-streaming, ``temperature: 0``,
and ``thinking: {"type": "disabled"}`` for the DeepSeek model.  Only the standard
library is used, so L2 adds no dependency to the submission.

Errors never echo the credential — not the key, not the header, not the URL
query string.  :class:`~loomq.errors.LLMConfigurationError` and
:class:`~loomq.errors.LLMTransportError` carry only the shape of the failure.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from ..errors import LLMConfigurationError, LLMTransportError

REQUIRED_ENV = ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY", "LOOMQ_LLM_MODEL")

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: Models known to need the explicit "no chain of thought" switch.
_THINKING_DISABLED_MODELS = ("deepseek-v4-flash",)


class LLMConfig(object):
    __slots__ = ("base_url", "api_key", "model", "timeout", "max_output_tokens")

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_output_tokens: int,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    def redacted(self) -> Dict[str, Any]:
        """Config with the credential removed, safe to log or return to a user."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout,
            "api_key": "set (%d characters)" % len(self.api_key),
        }


def load_config() -> LLMConfig:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise LLMConfigurationError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    try:
        timeout = float(os.environ.get("LOOMQ_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))
        max_output = int(
            os.environ.get("LOOMQ_LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)
        )
    except ValueError:
        raise LLMConfigurationError(
            "LOOMQ_LLM_TIMEOUT_SECONDS and LOOMQ_LLM_MAX_OUTPUT_TOKENS must be numeric"
        )
    if timeout <= 0 or max_output <= 0:
        raise LLMConfigurationError("timeout and output-token limit must be positive")
    return LLMConfig(
        base_url=os.environ["LOOMQ_LLM_BASE_URL"].rstrip("/"),
        api_key=os.environ["LOOMQ_LLM_API_KEY"],
        model=os.environ["LOOMQ_LLM_MODEL"],
        timeout=timeout,
        max_output_tokens=max_output,
    )


def is_configured() -> bool:
    return all(os.environ.get(name) for name in REQUIRED_ENV)


class LLMClient(object):
    """One client per ``agent_chat`` call; counts its own calls for the report."""

    def __init__(self, config: Optional[LLMConfig] = None, deadline: Optional[float] = None) -> None:
        self.config = config or load_config()
        self.calls = 0
        self.successful_calls = 0
        self.deadline = deadline

    def remaining(self) -> float:
        if self.deadline is None:
            return self.config.timeout
        return max(0.0, self.deadline - time.time())

    def complete(
        self,
        messages: Sequence[Dict[str, Any]],
        json_object: bool = False,
        max_tokens: Optional[int] = None,
        attempts: int = 2,
    ) -> str:
        """Return the assistant message content for one chat completion."""
        payload = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens or self.config.max_output_tokens,
        }  # type: Dict[str, Any]
        if self.config.model in _THINKING_DISABLED_MODELS:
            payload["thinking"] = {"type": "disabled"}
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        last_error = None  # type: Optional[str]
        for attempt in range(max(1, attempts)):
            budget = self.remaining()
            if budget <= 1.0:
                break
            self.calls += 1
            try:
                content = self._post(payload, timeout=min(self.config.timeout, budget))
                self.successful_calls += 1
                return content
            except LLMTransportError as exc:
                last_error = str(exc)
                if json_object and "response_format" in payload:
                    # Some gateways reject the JSON mode flag; retry without it
                    # rather than losing the case entirely.
                    payload.pop("response_format", None)
                if attempt + 1 < attempts:
                    time.sleep(min(1.0 + attempt, self.remaining()))
        raise LLMTransportError(last_error or "the model service could not be reached")

    def _post(self, payload: Dict[str, Any], timeout: float) -> str:
        request = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMTransportError("model service returned HTTP %d" % exc.code)
        except urllib.error.URLError:
            raise LLMTransportError("model service is unreachable")
        except (ValueError, TypeError):
            raise LLMTransportError("model service returned a non-JSON body")

        try:
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMTransportError("model response had no assistant message")
        if not isinstance(content, str) or not content.strip():
            raise LLMTransportError("model returned an empty message")
        return content


def extract_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Models wrap JSON in prose or fences even when asked not to, so the fence is
    stripped and the outermost balanced ``{...}`` is scanned for.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        newline = candidate.find("\n")
        candidate = candidate[newline + 1:] if newline != -1 else candidate
        fence = candidate.rfind("```")
        if fence != -1:
            candidate = candidate[:fence]
        candidate = candidate.strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(candidate):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    parsed = json.loads(candidate[start:index + 1])
                except ValueError:
                    start = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
    raise LLMTransportError("model reply contained no JSON object")


__all__ = [
    "DEFAULT_TIMEOUT",
    "LLMClient",
    "LLMConfig",
    "REQUIRED_ENV",
    "extract_json",
    "is_configured",
    "load_config",
]
