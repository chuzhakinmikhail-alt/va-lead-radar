"""LLM-based lead classifier.

Sends a candidate message to a Gemini or OpenAI chat-completions endpoint and
asks the model to decide whether it is a genuine service request or spam. The
model must answer with JSON: {"is_lead": bool, "summary": str, "intent": str}.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from .config import Config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — ассистент, который помогает digital-агентству находить клиентов. "
    "Определи, является ли сообщение реальным запросом на услуги (разработка, "
    "сайт, бот, дизайн, автоматизация и т.п.) или это спам/реклама/флуд/оффтоп. "
    "Верни ТОЛЬКО валидный JSON без markdown и пояснений в формате: "
    '{"is_lead": bool, "summary": str, "intent": str}. '
    "Где is_lead=true только для настоящих запросов на услуги. "
    "summary — краткое саммари запроса на русском (1-2 предложения). "
    "intent — короткая категория услуги (например: 'разработка бота', 'сайт', "
    "'дизайн', 'не релевантно')."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class LeadVerdict:
    is_lead: bool
    summary: str
    intent: str

    @classmethod
    def fallback(cls) -> "LeadVerdict":
        return cls(is_lead=False, summary="", intent="не удалось проанализировать")


class LLMError(RuntimeError):
    """Raised when the LLM request or response cannot be used."""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    text = (text or "").strip()
    if not text:
        raise LLMError("empty LLM response")

    # Strip common markdown code fences.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_RE.search(text)
    if not match:
        raise LLMError(f"no JSON object found in response: {text[:200]!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON in response: {text[:200]!r}") from exc


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "да"}
    return False


def parse_verdict(payload: dict) -> LeadVerdict:
    """Normalise a raw JSON dict into a :class:`LeadVerdict`."""
    summary = str(payload.get("summary") or "").strip()
    intent = str(payload.get("intent") or "").strip()
    return LeadVerdict(
        is_lead=_as_bool(payload.get("is_lead")),
        summary=summary,
        intent=intent or "не определено",
    )


class LeadAnalyzer:
    """Async client that classifies messages via a hosted LLM."""

    def __init__(self, config: Config, timeout: float = 30.0) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LeadAnalyzer":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def analyze(self, text: str) -> LeadVerdict:
        """Classify ``text``; never raises — returns a safe fallback on error."""
        text = (text or "").strip()
        if not text:
            return LeadVerdict.fallback()

        snippet = text[: self._config.max_analyze_chars]
        last_error: Exception | None = None
        for model in self._config.models:
            try:
                if self._config.llm_provider == "openai":
                    raw = await self._call_openai(snippet, model)
                else:
                    raw = await self._call_gemini(snippet, model)
                verdict = parse_verdict(_extract_json(raw))
                logger.info(
                    "LLM verdict (model=%s): is_lead=%s intent=%r",
                    model,
                    verdict.is_lead,
                    verdict.intent,
                )
                return verdict
            except (httpx.HTTPError, LLMError) as exc:
                last_error = exc
                logger.warning("LLM model %s failed: %s", model, exc)
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
                logger.exception("Unexpected LLM error on %s: %s", model, exc)

        logger.error("All LLM models failed; last error: %s", last_error)
        return LeadVerdict.fallback()

    # ---- providers -----------------------------------------------------
    async def _call_gemini(self, text: str, model: str) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        resp = await self._client.post(
            url,
            params={"key": self._config.llm_api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected Gemini response shape: {data}") from exc

    async def _call_openai(self, text: str, model: str) -> str:
        url = f"{self._config.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        resp = await self._client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._config.llm_api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected OpenAI response shape: {data}") from exc
