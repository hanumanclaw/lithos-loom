"""Unit tests for coder-turn result parsing."""

from __future__ import annotations

import json

from lithos_loom.plugins.story_develop.turns import parse_claude_result

_SUCCESS = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "OK",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "total_cost_usd": 0.1928,
    }
)


def test_parse_success() -> None:
    r = parse_claude_result(_SUCCESS, exit_code=0, stderr="")
    assert r.succeeded is True
    assert r.session_id == "11111111-2222-3333-4444-555555555555"
    assert r.result_text == "OK"
    assert r.cost_usd == 0.1928


def test_parse_is_error_fails_even_with_zero_exit() -> None:
    payload = json.dumps({"type": "result", "is_error": True, "result": "limit"})
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.succeeded is False


def test_parse_nonzero_exit_fails() -> None:
    r = parse_claude_result(_SUCCESS, exit_code=1, stderr="boom")
    assert r.succeeded is False
    assert r.stderr == "boom"


def test_parse_garbage_output_fails_safely() -> None:
    r = parse_claude_result("not json", exit_code=0, stderr="")
    assert r.succeeded is False
    assert r.raw is None
    assert r.cost_usd == 0.0


def test_parse_empty_output_fails_safely() -> None:
    r = parse_claude_result("", exit_code=0, stderr="")
    assert r.succeeded is False
    assert r.raw is None


def test_parse_requires_session_id_for_success() -> None:
    payload = json.dumps({"type": "result", "is_error": False, "result": "OK"})
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.succeeded is False  # no session_id -> not a usable success
    assert r.session_id == ""


def test_parse_normalises_null_fields_not_to_literal_none() -> None:
    payload = json.dumps(
        {"type": "result", "is_error": False, "result": None, "session_id": "s1"}
    )
    r = parse_claude_result(payload, exit_code=0, stderr="")
    assert r.result_text == ""  # not "None"
    assert r.succeeded is True
