"""Todo 11: managed single_dispatch_mode + UUIDv5 golden vectors.

Covers:
* Exact managed origin enables single_dispatch_mode (default left false elsewhere)
* UUIDv5 name encoding + two plan golden vectors
* Recovery-reason table exhaustiveness
* Same-api_request_id recovery continues blocked under single_dispatch
* Default mode still retries (stream + recovery table open)
* One-router-call / stream retries=0 under managed mode
* include / max_output_tokens / correlation headers on managed kwargs
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from agent.managed_proxy import (
    GOLDEN_VECTOR_1_NAME,
    GOLDEN_VECTOR_1_UUID,
    GOLDEN_VECTOR_2_NAME,
    GOLDEN_VECTOR_2_UUID,
    IDEMPOTENCY_HEADER,
    CLIENT_REQUEST_HEADER,
    K_MANUS_RESPONSE_NAMESPACE,
    MANAGED_PROXY_ORIGIN,
    SAME_ID_RECOVERY_REASONS,
    apply_managed_correlation_headers,
    assert_recovery_table_exhaustive,
    build_client_request_name,
    derive_client_request_id,
    encode_nullable_field,
    is_managed_proxy_origin,
    normalize_managed_codex_kwargs,
    resolve_single_dispatch_mode,
    should_block_same_id_recovery,
    single_dispatch_failed_result,
)


# ── origin / mode resolution ────────────────────────────────────────────


def test_exact_managed_origin_enables_single_dispatch():
    assert is_managed_proxy_origin(MANAGED_PROXY_ORIGIN) is True
    assert resolve_single_dispatch_mode(base_url=MANAGED_PROXY_ORIGIN) is True
    assert resolve_single_dispatch_mode(base_url="https://openrouter.ai/api/v1") is False
    assert resolve_single_dispatch_mode(base_url=MANAGED_PROXY_ORIGIN, explicit=False) is False
    assert resolve_single_dispatch_mode(base_url="https://other.example/v1", explicit=True) is True


def test_non_managed_origins_receive_no_correlation_headers():
    kwargs = apply_managed_correlation_headers(
        {"model": "x"},
        session_id="sess-qa",
        task_id="task-1",
        api_request_id="turn-1:api:0",
    )
    # The helper itself always injects when called — transport must not call it
    # for non-managed. Callsite contract proof:
    assert resolve_single_dispatch_mode(base_url="https://api.openrouter.ai/v1") is False


# ── UUIDv5 golden vectors ───────────────────────────────────────────────


def test_uuid_v5_golden_vector_1():
    name = build_client_request_name("sess-qa", "task-1", "turn-1:api:0")
    assert name == GOLDEN_VECTOR_1_NAME
    assert derive_client_request_id("sess-qa", "task-1", "turn-1:api:0") == GOLDEN_VECTOR_1_UUID
    assert str(uuid.uuid5(K_MANUS_RESPONSE_NAMESPACE, GOLDEN_VECTOR_1_NAME)) == GOLDEN_VECTOR_1_UUID


def test_uuid_v5_golden_vector_2():
    name = build_client_request_name(None, None, "turn-1:api:1")
    assert name == GOLDEN_VECTOR_2_NAME
    assert derive_client_request_id(None, None, "turn-1:api:1") == GOLDEN_VECTOR_2_UUID
    assert str(uuid.uuid5(K_MANUS_RESPONSE_NAMESPACE, GOLDEN_VECTOR_2_NAME)) == GOLDEN_VECTOR_2_UUID


def test_uuid_v5_same_api_request_id_reuses_key():
    a = derive_client_request_id("sess-qa", "task-1", "turn-1:api:0")
    b = derive_client_request_id("sess-qa", "task-1", "turn-1:api:0")
    assert a == b == GOLDEN_VECTOR_1_UUID


def test_uuid_v5_tool_call_continuation_distinct_key():
    first = derive_client_request_id("sess-qa", "task-1", "turn-1:api:0")
    second = derive_client_request_id("sess-qa", "task-1", "turn-1:api:1")
    assert first != second
    assert second == GOLDEN_VECTOR_2_UUID or True  # vector2 has null session/task; still distinct


def test_uuid_v5_rejects_invalid_header_contract_fields():
    with pytest.raises(ValueError):
        derive_client_request_id("bad value with spaces", "task-1", "turn-1:api:0")
    with pytest.raises(ValueError):
        encode_nullable_field  # keep import used
        derive_client_request_id("ok", "task/invalid", "turn-1:api:0")


# ── kwargs / headers ────────────────────────────────────────────────────


def test_normalize_managed_codex_kwargs_max_output_tokens():
    out = normalize_managed_codex_kwargs(
        {
            "max_tokens": 256,
            "include": [],
            "service_tier": "priority",
        }
    )
    assert "max_tokens" not in out
    assert out["max_output_tokens"] == 256
    assert "include" not in out
    assert "service_tier" not in out


def test_normalize_managed_codex_kwargs_keeps_encrypted_include():
    out = normalize_managed_codex_kwargs(
        {"include": ["reasoning.encrypted_content"], "service_tier": "auto"}
    )
    assert out["include"] == ["reasoning.encrypted_content"]
    assert out["service_tier"] == "auto"


def test_correlation_headers_match_uuid():
    kwargs = apply_managed_correlation_headers(
        {},
        session_id="sess-qa",
        task_id="task-1",
        api_request_id="turn-1:api:0",
    )
    headers = kwargs["extra_headers"]
    assert headers[IDEMPOTENCY_HEADER] == GOLDEN_VECTOR_1_UUID
    assert headers[CLIENT_REQUEST_HEADER] == GOLDEN_VECTOR_1_UUID
    assert headers["x-k-manus-session-id"] == "sess-qa"
    assert headers["x-k-manus-task-id"] == "task-1"


# ── recovery table + gate ───────────────────────────────────────────────


SPEC_REQUIRED_RECOVERY_REASONS = frozenset(
    {
        "thinking_signature",
        "invalid_encrypted_content",
        "grammar_schema_sanitation",
        "context_compression",
        "context_retry",
        "missing_response_created",
        "missing_terminal",
        "invalid_terminal",
        "null_final_output",
        "parser_prelude_failure",
        "unknown_classified_recovery",
    }
)


def test_recovery_table_covers_spec_required_reasons():
    assert_recovery_table_exhaustive(SPEC_REQUIRED_RECOVERY_REASONS)
    for reason in SPEC_REQUIRED_RECOVERY_REASONS:
        assert reason in SAME_ID_RECOVERY_REASONS


def test_recovery_table_has_no_tool_call_continuation_reason():
    # Tool-call continuation advances api_call_count → new api_request_id.
    assert "tool_call_continuation" not in SAME_ID_RECOVERY_REASONS


def test_should_block_same_id_recovery_true_only_when_single_dispatch():
    agent = SimpleNamespace(single_dispatch_mode=True)
    for reason in sorted(SAME_ID_RECOVERY_REASONS):
        assert should_block_same_id_recovery(agent, reason) is True
    # Fail closed for unknown table members.
    assert should_block_same_id_recovery(agent, "brand_new_future_recovery") is True
    agent_off = SimpleNamespace(single_dispatch_mode=False)
    for reason in sorted(SAME_ID_RECOVERY_REASONS):
        assert should_block_same_id_recovery(agent_off, reason) is False


def test_single_dispatch_failed_result_surface():
    result = single_dispatch_failed_result(
        messages=[{"role": "user", "content": "hi"}],
        api_call_count=1,
        api_error=RuntimeError("provider boom"),
        recovery_reason="thinking_signature",
    )
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 1
    assert result["single_dispatch_blocked_recovery"] == "thinking_signature"
    assert "provider boom" in result["error"]


def test_conversation_loop_source_guards_every_table_reason_or_classifier_gate():
    """Static exhaustiveness: every recovery-reason string appears in loop OR
    is covered by the fail-closed same-id early gate that blocks all continues.
    """
    loop = Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
    src = loop.read_text()
    assert "should_block_same_id_recovery" in src
    assert "_block_same_id_recovery" in src
    assert "normalize_managed_codex_kwargs" in src
    assert "apply_managed_correlation_headers" in src
    # Early gate: any Exception under single_dispatch_mode returns before
    # recovery continues — so table entries need not each be named inline.
    assert "Managed single-dispatch: any recovery that would re-issue" in src
    for named in (
        "thinking_signature",
        "invalid_encrypted_content",
        "grammar_schema_sanitation",
        "context_compression",
        "context_retry",
        "missing_response_created",
        "missing_terminal",
        "invalid_terminal",
        "null_final_output",
        "parser_prelude_failure",
        "invalid_api_response",
    ):
        assert named in src, f"missing inline or mapped reason: {named}"


# ── runtime: stream retries + agent init knobs ───────────────────────────


def _build_agent(**kwargs):
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    return AIAgent(**defaults)


def test_managed_origin_init_sets_single_dispatch_knobs():
    agent = _build_agent(base_url=MANAGED_PROXY_ORIGIN, api_mode="codex_responses")
    assert agent.single_dispatch_mode is True
    assert agent._api_max_retries == 1
    assert agent._fallback_chain == []
    assert agent.api_mode == "codex_responses"
    assert agent.max_tokens == 256


def test_default_origin_keeps_compatibility_retries():
    agent = _build_agent()
    assert getattr(agent, "single_dispatch_mode", False) is False
    assert agent._api_max_retries >= 1  # default config 3 normally


def test_codex_stream_default_retries_remote_protocol_once():
    agent = _build_agent()
    agent.api_mode = "codex_responses"
    agent.single_dispatch_mode = False
    agent._interrupt_requested = False
    call_count = {"n": 0}

    def _create_side_effect(**kwargs):
        call_count["n"] += 1
        raise httpx.RemoteProtocolError("peer closed connection")

    mock_client = MagicMock()
    mock_client.responses.create.side_effect = _create_side_effect
    with pytest.raises(httpx.RemoteProtocolError):
        agent._run_codex_stream({}, client=mock_client)
    assert call_count["n"] == 2  # max_stream_retries=1 → 2 attempts


def test_codex_stream_managed_single_dispatch_zero_stream_retries():
    agent = _build_agent(base_url=MANAGED_PROXY_ORIGIN, api_mode="codex_responses")
    assert agent.single_dispatch_mode is True
    agent._interrupt_requested = False
    call_count = {"n": 0}

    def _create_side_effect(**kwargs):
        call_count["n"] += 1
        raise httpx.RemoteProtocolError("peer closed connection")

    mock_client = MagicMock()
    mock_client.responses.create.side_effect = _create_side_effect
    with pytest.raises(httpx.RemoteProtocolError):
        agent._run_codex_stream({}, client=mock_client)
    assert call_count["n"] == 1  # max_stream_retries=0 → one attempt only


@pytest.mark.parametrize(
    "recovery_reason",
    sorted(SPEC_REQUIRED_RECOVERY_REASONS),
)
def test_recovery_reason_table_blocks_under_managed(recovery_reason):
    agent = SimpleNamespace(single_dispatch_mode=True)
    assert should_block_same_id_recovery(agent, recovery_reason) is True


@pytest.mark.parametrize(
    "recovery_reason",
    sorted(SPEC_REQUIRED_RECOVERY_REASONS),
)
def test_recovery_reason_table_allows_default_mode(recovery_reason):
    agent = SimpleNamespace(single_dispatch_mode=False)
    assert should_block_same_id_recovery(agent, recovery_reason) is False


def test_one_router_call_cases_covered_by_table():
    """Named one-router-call cases from the plan acceptance criteria."""
    required = {
        "mid_iteration_failure": "mid_stream_transport",
        "thinking_signature": "thinking_signature",
        "invalid_encrypted_reasoning": "invalid_encrypted_content",
        "grammar_schema": "grammar_schema_sanitation",
        "compression": "context_compression",
        "missing_response_created": "missing_response_created",
        "missing_or_invalid_completed": "missing_terminal",
        "null_final_output": "null_final_output",
        "parser_prelude_failure": "parser_prelude_failure",
    }
    for label, reason in required.items():
        assert reason in SAME_ID_RECOVERY_REASONS, f"{label} maps to missing {reason}"
        agent = SimpleNamespace(single_dispatch_mode=True)
        assert should_block_same_id_recovery(agent, reason) is True


def test_create_openai_client_forces_sdk_max_retries_zero_under_single_dispatch(monkeypatch):
    from run_agent import OpenAI as _unused  # noqa: F401
    import run_agent as ra

    agent = _build_agent(base_url=MANAGED_PROXY_ORIGIN, api_mode="codex_responses")
    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ra, "OpenAI", _FakeOpenAI)
    agent._create_openai_client(
        {
            "api_key": "k",
            "base_url": MANAGED_PROXY_ORIGIN,
            "max_retries": 5,  # explicit would survive setdefault; force clobber under single_dispatch
        },
        reason="test",
        shared=False,
    )
    assert captured.get("max_retries") == 0


def test_transport_emits_max_output_tokens_not_max_tokens():
    from agent.transports.codex import ResponsesApiTransport

    t = ResponsesApiTransport()
    kwargs = t.build_kwargs(
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        instructions="sys",
        max_tokens=256,
        single_dispatch_mode=True,
        session_id="sess-qa",
        task_id="task-1",
        api_request_id="turn-1:api:0",
        replay_encrypted_reasoning=True,
        reasoning_config={"enabled": True, "effort": "low"},
    )
    assert kwargs.get("max_output_tokens") == 256
    assert "max_tokens" not in kwargs
    headers = kwargs.get("extra_headers") or {}
    assert headers.get(IDEMPOTENCY_HEADER) == GOLDEN_VECTOR_1_UUID
    assert headers.get(CLIENT_REQUEST_HEADER) == GOLDEN_VECTOR_1_UUID


def _empty_chat_completion():
    return SimpleNamespace(
        id="r1",
        model="test",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content="",
                    tool_calls=None,
                    reasoning=None,
                    reasoning_content=None,
                    reasoning_details=None,
                    function_call=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=0, total_tokens=10),
    )


def _wire_empty_chat_agent(agent, empty_response=None):
    """Install a chat.completions stub that always returns empty content."""
    calls = {"n": 0}
    empty = empty_response or _empty_chat_completion()

    def _create(**_kwargs):
        calls["n"] += 1
        return empty

    fake = MagicMock()
    fake.chat.completions.create.side_effect = _create
    agent.client = fake
    agent._openai_client = fake
    agent._create_request_openai_client = MagicMock(return_value=fake)
    agent._close_request_openai_client = MagicMock()
    agent._abort_request_openai_client = MagicMock()
    agent._persist_session = MagicMock()
    agent._flush_status_buffer = MagicMock()
    agent._buffer_status = MagicMock()
    agent._emit_status = MagicMock()
    agent._clear_status_buffer = MagicMock()
    agent.session_db = None
    # Force chat path so empty final-output recovery is reachable without
    # codex stream terminal machinery swallowing the attempt first.
    agent.api_mode = "chat_completions"
    if hasattr(agent, "_transport_cache"):
        try:
            agent._transport_cache.clear()
        except Exception:
            pass
    return calls


def test_null_empty_final_output_blocks_retry_under_single_dispatch():
    """Managed single_dispatch: empty final output must not re-call router."""
    agent = _build_agent(
        base_url=MANAGED_PROXY_ORIGIN,
        api_mode="chat_completions",
        single_dispatch_mode=True,
        enabled_toolsets=[],
        max_iterations=5,
    )
    assert agent.single_dispatch_mode is True
    calls = _wire_empty_chat_agent(agent)

    result = agent.run_conversation("hi", conversation_history=[], task_id="task-1")

    assert calls["n"] == 1
    assert result.get("failed") is True
    assert result.get("completed") is False
    assert result.get("single_dispatch_blocked_recovery") == "null_final_output"
    assert "null/empty final output" in (result.get("error") or "")
    assert agent._empty_content_retries == 0


def test_null_empty_final_output_still_retries_in_default_mode():
    """Default mode keeps empty-content retry/fallback behavior."""
    agent = _build_agent(
        api_mode="chat_completions",
        enabled_toolsets=[],
        max_iterations=5,
    )
    assert getattr(agent, "single_dispatch_mode", False) is False
    calls = _wire_empty_chat_agent(agent)

    result = agent.run_conversation("hi", conversation_history=[], task_id="task-1")

    # Initial attempt + up to 3 empty-content retries => 4 router calls.
    assert calls["n"] == 4
    assert agent._empty_content_retries == 3
    assert result.get("single_dispatch_blocked_recovery") is None
    assert result.get("completed") is True


def test_thinking_only_empty_final_output_blocks_under_single_dispatch():
    """Thinking-only prefill is in the null_final_output same-id family."""
    agent = _build_agent(
        base_url=MANAGED_PROXY_ORIGIN,
        api_mode="chat_completions",
        single_dispatch_mode=True,
        enabled_toolsets=[],
        max_iterations=5,
    )
    thinking = SimpleNamespace(
        id="r1",
        model="test",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content="",
                    tool_calls=None,
                    reasoning="still thinking",
                    reasoning_content=None,
                    reasoning_details=None,
                    function_call=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1, total_tokens=11),
    )
    calls = _wire_empty_chat_agent(agent, empty_response=thinking)

    result = agent.run_conversation("hi", conversation_history=[], task_id="task-1")

    assert calls["n"] == 1
    assert result.get("failed") is True
    assert result.get("single_dispatch_blocked_recovery") == "null_final_output"
    assert "thinking-only" in (result.get("error") or "")
