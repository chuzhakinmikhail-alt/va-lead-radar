"""Unit tests for the LLM response parser (no network required)."""

from app.analyzer import LeadVerdict, _extract_json, parse_verdict


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
