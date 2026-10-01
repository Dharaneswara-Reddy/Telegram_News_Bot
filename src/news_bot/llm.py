"""
Groq chat client tuned for the free tier.

Each Groq model has its own per-minute token bucket (8K TPM on the free
tier), so rotating across several models multiplies throughput. The client
tracks a cooldown per model from the rate-limit headers and 429 responses,
always uses whichever model frees up first, and gives up cleanly when the
run's time budget is spent — unsummarised items simply wait for the next run.

Groq also retires model ids without much notice. At start-up the configured
list is intersected with the live ``/models`` listing so a retirement
degrades to "use the next model" instead of an outage.
"""

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("news-bot.llm")

API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
# Families worth falling back to if every configured id has been retired.
_FALLBACK_FAMILIES = re.compile(r"gpt-oss|qwen|llama|kimi|deepseek|mistral", re.IGNORECASE)
_NOT_CHAT = re.compile(r"whisper|guard|orpheus|tts|allam", re.IGNORECASE)


def _duration(value: str | None) -> float:
    """Parse Groq's reset durations such as ``1m26.4s``, ``3.5s`` or ``250ms``."""
    if not value:
        return 0.0
    total = 0.0
    for amount, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        total += float(amount) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


class LLMUnavailableError(RuntimeError):
    pass


class GroqClient:
    def __init__(
        self,
        api_key: str,
        models: list[str] | None = None,
        deadline: float | None = None,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key
        self.models = list(models or DEFAULT_MODELS)
        self.deadline = deadline  # time.monotonic() value after which we stop
        self.session = session or requests.Session()
        self.cooldown: dict[str, float] = {}
        self.exhausted: set[str] = set()
        self.calls = 0
        self.tokens = 0

    @classmethod
    def from_env(cls, deadline: float | None = None) -> "GroqClient | None":
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            return None
        models = [m for m in [os.environ.get("GROQ_MODEL")] if m]
        extra = os.environ.get("GROQ_FALLBACK_MODELS")
        models += extra.split(",") if extra else DEFAULT_MODELS
        client = cls(key, list(dict.fromkeys(m.strip() for m in models if m.strip())), deadline)
        client.refresh_live_models()
        return client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def refresh_live_models(self) -> None:
        try:
            resp = self.session.get(f"{API_BASE}/models", headers=self._headers(), timeout=20)
            resp.raise_for_status()
            live = {m["id"] for m in resp.json().get("data", []) if m.get("active", True)}
        except Exception as exc:
            log.warning("Could not list Groq models (%s); using configured list as-is.", exc)
            return
        usable = [m for m in self.models if m in live]
        if not usable:
            usable = sorted(
                m for m in live if _FALLBACK_FAMILIES.search(m) and not _NOT_CHAT.search(m)
            )
            log.warning("None of %s is live on Groq; falling back to %s", self.models, usable)
        elif len(usable) < len(self.models):
            log.warning("Groq retired %s; continuing with %s", set(self.models) - live, usable)
        self.models = usable

    def _time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def _next_model(self) -> str:
        available = [m for m in self.models if m not in self.exhausted]
        if not available:
            raise LLMUnavailableError("every Groq model is retired or out of daily quota")
        model = min(available, key=lambda m: self.cooldown.get(m, 0.0))
        wait = self.cooldown.get(model, 0.0) - time.monotonic()
        if wait > 0:
            if wait > self._time_left():
                raise LLMUnavailableError("run time budget spent waiting for rate limits")
            log.info("Rate limited on all models; waiting %.1fs", wait)
            time.sleep(wait)
        return model

    def _note_limits(self, model: str, resp: requests.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining-tokens")
        try:
            low = remaining is not None and float(remaining) < 3500
        except ValueError:
            low = False
        if low:
            reset = _duration(resp.headers.get("x-ratelimit-reset-tokens")) or 10.0
            self.cooldown[model] = time.monotonic() + reset
        if resp.headers.get("x-ratelimit-remaining-requests") == "0":
            self.exhausted.add(model)

    def chat_json(self, system: str, user: str, max_tokens: int = 3000, attempts: int = 6) -> dict:
        """Return the model's reply parsed as a JSON object.

        Raises LLMUnavailableError when no model can answer within the budget.
        """
        last_error = "no attempt made"
        for _ in range(attempts):
            if self._time_left() <= 5:
                raise LLMUnavailableError("run time budget spent")
            model = self._next_model()
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            if "gpt-oss" in model:
                # Reasoning tokens count against max_tokens and the TPM bucket.
                body["reasoning_effort"] = "low"
            try:
                resp = self.session.post(
                    f"{API_BASE}/chat/completions", headers=self._headers(), json=body, timeout=90
                )
            except requests.RequestException as exc:
                last_error = f"{model}: {exc}"
                self.cooldown[model] = time.monotonic() + 5
                continue

            self.calls += 1
            if resp.status_code == 429:
                text = resp.text[:300]
                if "per day" in text or "(RPD)" in text or "(TPD)" in text:
                    log.warning("%s hit its daily limit: %s", model, text)
                    self.exhausted.add(model)
                else:
                    try:
                        wait = float(resp.headers.get("retry-after") or 0)
                    except ValueError:
                        wait = 0.0
                    self.cooldown[model] = time.monotonic() + (wait or 15.0)
                last_error = f"{model}: 429"
                continue
            if resp.status_code == 404 or (
                resp.status_code == 400 and "decommissioned" in resp.text
            ):
                log.warning("%s unavailable (%s); dropping it.", model, resp.text[:200])
                self.exhausted.add(model)
                last_error = f"{model}: retired"
                continue
            if resp.status_code >= 400:
                # 400 json_validate_failed and 5xx: try again, preferably elsewhere.
                last_error = f"{model}: HTTP {resp.status_code} {resp.text[:200]}"
                log.warning("Groq error %s", last_error)
                self.cooldown[model] = time.monotonic() + 3
                continue

            self._note_limits(model, resp)
            payload = resp.json()
            self.tokens += (payload.get("usage") or {}).get("total_tokens", 0)
            content = payload["choices"][0]["message"].get("content") or ""
            try:
                return parse_json_object(content)
            except ValueError:
                last_error = f"{model}: unparsable reply {content[:120]!r}"
                log.warning("Groq returned non-JSON from %s", model)
                continue
        raise LLMUnavailableError(last_error)


def parse_json_object(content: str) -> dict:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in reply") from None
        try:
            value = json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from None
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    return value
