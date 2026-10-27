import json
from types import SimpleNamespace

import httpx2
import pytest

from jobhelper import llm

from .conftest import ASSESSMENT, FakeClaude, message, sse


def test_score_job_request_shape(fake_claude):
    usage = llm.Usage()
    result = llm.score_job(fake_claude.client, "claude-opus-5-5", "low", "<resume>...</resume>", "<job_posting/>", usage)
    assert result.score == 81 and result.gaps == ["No Go"]

    [(request, body)] = fake_claude.requests
    assert request.headers["anthropic-beta"] == llm.FALLBACK_BETA
    assert body["fallbacks"] == "default"
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in body
    [system] = body["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert system["text"].startswith(llm.SCORE_INSTRUCTIONS) and system["text"].endswith("<resume>...</resume>")
    assert body["messages"] == [{"role": "user", "content": "<job_posting/>"}]

    # 200 input + 3000 cached + 50 output at Opus 5.5 prices
    assert usage.cost == pytest.approx((200 * 4 + 3000 * 0.20 + 50 * 20) / 1e6)
    assert "3,000 from cache" in usage.summary()


def test_haiku_gets_no_fallback_parameters():
    fake = FakeClaude()
    llm.score_job(fake.client, "claude-haiku-5-5", "low", "profile", "job", llm.Usage())
    [(request, body)] = fake.requests
    assert "fallbacks" not in body and "anthropic-beta" not in request.headers


def test_scores_are_clamped():
    fake = FakeClaude(lambda body: httpx2.Response(200, json=message([{"type": "text", "text": json.dumps({**ASSESSMENT, "score": 140})}])))
    assert llm.score_job(fake.client, "claude-opus-5-5", "low", "p", "j", llm.Usage()).score == 100


def test_refusal_raises():
    fake = FakeClaude(lambda body: httpx2.Response(200, json=message([], stop_reason="refusal")))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.score_job(fake.client, "claude-opus-5-5", "low", "p", "j", llm.Usage())


def test_prep_streams_and_returns_text():
    fake = FakeClaude(lambda body: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse("## Snapshot\n- Fit")))
    usage = llm.Usage()
    text = llm.write_prep(fake.client, "claude-opus-5-5", "high", "profile", "<job_posting/>", usage)
    assert text == "## Snapshot\n- Fit"
    [(_, body)] = fake.requests
    assert body["stream"] is True and body["output_config"] == {"effort": "high"}
    assert body["system"][0]["text"].startswith(llm.PREP_INSTRUCTIONS)
    assert usage.calls == 1 and usage.output_tokens == 40


def test_truncated_output_raises():
    fake = FakeClaude(lambda body: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse("## Snap", "max_tokens")))
    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.write_review(fake.client, "claude-opus-5-5", "high", "profile", llm.Usage())


def test_final_text_keeps_only_the_model_that_finished():
    reply = SimpleNamespace(content=[
        SimpleNamespace(type="text", text="partial from the first model"),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="text", text="## Snapshot"),
    ])
    assert llm._final_text(reply) == "## Snapshot"


def test_missing_credentials_message(monkeypatch, tmp_path):
    monkeypatch.setattr(llm.Path, "home", lambda: tmp_path)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(llm.LLMError, match=".env"):
        llm.make_client()
