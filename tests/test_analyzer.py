"""Unit tests for the LLM response parser and the model fallback chain."""

import asyncio
import json

import httpx

from app.analyzer import LeadAnalyzer, LeadVerdict, _extract_json, parse_verdict
from app.config import Config


def test_extract_plain_json():
    assert _extract_json('{"is_lead": true}') == {"is_lead": True}


def test_extract_json_from_markdown_fence():
    raw = '```json\n{"is_lead": false, "summary": "s", "intent": "i"}\n```'
    assert _extract_json(raw) == {"is_lead": False, "summary": "s", "intent": "i"}


def test_extract_json_with_surrounding_prose():
    raw = 'Sure! {"is_lead": true, "summary": "нужен бот", "intent": "бот"} hope it helps'
    parsed = _extract_json(raw)
    assert parsed["is_lead"] is True


def test_parse_verdict_normalises_types():
    verdict = parse_verdict({"is_lead": "true", "summary": "  нужен сайт ", "intent": ""})
    assert isinstance(verdict, LeadVerdict)
    assert verdict.is_lead is True
    assert verdict.summary == "нужен сайт"
    assert verdict.intent == "не определено"


def test_parse_verdict_missing_fields_defaults_false():
    verdict = parse_verdict({})
    assert verdict.is_lead is False
    assert verdict.summary == ""


def _config(**overrides) -> Config:
    base = {
        "bot_token": "1:x",
        "admin_id": 1,
        "llm_api_key": "test-key",
        "llm_provider": "openai",
        "llm_model": "primary-model",
        "openai_base_url": "https://llm.test/v1",
    }
    base.update(overrides)
    return Config(**base)


def test_models_dedupes_and_keeps_order():
    cfg = _config(llm_fallback_models=("second", "primary-model", "", "third"))
    assert cfg.models == ("primary-model", "second", "third")


def test_analyzer_falls_back_to_next_model_on_rate_limit():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "primary-model":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"is_lead": true, "summary": "нужен бот", '
                            '"intent": "разработка бота"}'
                        }
                    }
                ]
            },
        )

    analyzer = LeadAnalyzer(_config(llm_fallback_models=("backup-model",)))
    analyzer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        async with analyzer:
            return await analyzer.analyze("Ищу разработчика бота")

    verdict = asyncio.run(run())
    assert seen == ["primary-model", "backup-model"]
    assert verdict.is_lead is True
    assert verdict.intent == "разработка бота"


def test_analyzer_returns_fallback_when_all_models_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    analyzer = LeadAnalyzer(_config(llm_fallback_models=("backup-model",)))
    analyzer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        async with analyzer:
            return await analyzer.analyze("Ищу разработчика")

    verdict = asyncio.run(run())
    assert verdict.is_lead is False
    assert verdict.intent == "не удалось проанализировать"
