"""Managed Responses proxy helpers for K-manus desktop clients.

When Hermes points at the exact managed origin it runs in
``single_dispatch_mode``: at most one router HTTP call per ``api_request_id``,
no stream/outer/SDK retries for that request, and no model-fallback chain.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Iterable, Optional, Sequence
from urllib.parse import urlsplit

# Exact managed origin (scheme + host + path including trailing /v1).
# TEMPORARY domain: today this host serves the legacy CLI proxy, and the final
# managed origin is an owner Ready-time decision (same-origin cutover vs
# router.k-manus.app vs configurable). Until then managed turns fail closed at
# this origin. Keep in lockstep with the desktop pin
# (k-manus apps/desktop/src-tauri/src/membership/managed_proxy.rs and
# hermes/event_runner.py) — the two sides reject any mismatch.
MANAGED_PROXY_ORIGIN = "https://proxy.nomadamas.org/v1"

# Literal UUIDv5 namespace for Idempotency-Key / x-k-manus-client-request-id.
K_MANUS_RESPONSE_NAMESPACE = uuid.UUID("6f0f4d78-49a0-5b16-9d1e-8d05c7a4a0b1")
K_MANUS_RESPONSE_NAME_PREFIX = "k-manus-response-v1|"

SESSION_HEADER = "x-k-manus-session-id"
TASK_HEADER = "x-k-manus-task-id"
CLIENT_REQUEST_HEADER = "x-k-manus-client-request-id"
IDEMPOTENCY_HEADER = "Idempotency-Key"

# ASCII header contract from model-router: 1..128 of A-Za-z0-9._:-
HEADER_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Exhaustive table of recovery reasons that would ``continue`` the API-call
# loop and issue another HTTP call for the SAME ``api_request_id``.  Tool-call
# continuation is intentionally absent — it advances ``api_call_count`` and
# mints a distinct id.  Guard tests assert every entry is covered here.
SAME_ID_RECOVERY_REASONS: frozenset[str] = frozenset(
    {
        "thinking_signature",
        "invalid_encrypted_content",
        "llama_cpp_grammar_pattern",
        "grammar_schema_sanitation",
        "context_compression",
        "context_retry",
        "missing_response_created",
        "missing_terminal",
        "invalid_terminal",
        "null_final_output",
        "parser_prelude_failure",
        "mid_stream_transport",
        "stream_retry",
        "invalid_api_response",
        "credential_pool_rotation",
        "auth_refresh",
        "fallback_provider",
        "image_shrink",
        "multimodal_tool_content",
        "oauth_long_context_beta",
        "unicode_sanitization",
        "image_rejection",
        "primary_transport_recovery",
        "rate_limit_retry",
        "billing_retry",
        "timeout_retry",
        "unknown_classified_recovery",
        # Future classified recoveries must occupy this table; the guard test
        # fails closed if a new recovery continues outside this set under
        # single_dispatch_mode.
    }
)


def normalize_origin(base_url: Optional[str]) -> str:
    """Normalize a base URL to scheme://host[:port]/path-without-trailing-slash.

    Effective ports 443 (https) and 80 (http) are omitted so APIs that include
    or omit the default port still match the managed origin string.
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path.rstrip("/") or ""
    return f"{scheme}://{netloc}{path}"


def is_managed_proxy_origin(base_url: Optional[str]) -> bool:
    """True only for the exact managed Responses proxy origin."""
    return normalize_origin(base_url) == normalize_origin(MANAGED_PROXY_ORIGIN)


def resolve_single_dispatch_mode(
    *,
    base_url: Optional[str] = None,
    explicit: Optional[bool] = None,
) -> bool:
    """Resolve typed transport option ``single_dispatch_mode``.

    Explicit ``True``/``False`` wins; otherwise the managed origin enables it.
    Default compatibility path is False.
    """
    if explicit is not None:
        return bool(explicit)
    return is_managed_proxy_origin(base_url)


def encode_nullable_field(value: Optional[str]) -> str:
    """Encode one nullable field for the UUIDv5 name (no normalization)."""
    if value is None:
        return "N;"
    if not isinstance(value, str):
        raise TypeError("correlation field must be str or None")
    raw = value.encode("utf-8")
    return f"S{len(raw):08x}:{value};"


def build_client_request_name(
    session_id: Optional[str],
    task_id: Optional[str],
    api_request_id: Optional[str],
) -> str:
    """Build the UUIDv5 name for ``(session_id, task_id, api_request_id)``."""
    return (
        K_MANUS_RESPONSE_NAME_PREFIX
        + encode_nullable_field(session_id)
        + encode_nullable_field(task_id)
        + encode_nullable_field(api_request_id)
    )


def validate_correlation_field(name: str, value: Optional[str]) -> Optional[str]:
    """Reject values that the model-router header contract would reject.

    Returns the value on success (or None). Raises ``ValueError`` on reject.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    if value == "" or value != value.strip():
        raise ValueError(f"{name} rejected by header contract")
    if not HEADER_VALUE_RE.match(value):
        raise ValueError(f"{name} rejected by header contract")
    return value


def derive_client_request_id(
    session_id: Optional[str],
    task_id: Optional[str],
    api_request_id: Optional[str],
    *,
    validate: bool = True,
) -> str:
    """UUIDv5 over fixed namespace + encoded nullable fields. Lowercase str.

    Only ``session_id`` and ``task_id`` are validated against the header
    contract — they travel as raw header values. ``api_request_id`` is never
    sent as a header (it is only UUIDv5 name material, and it embeds the
    other two fields so it can legitimately exceed the 128-char header
    limit); it is hashed as-is.
    """
    if validate:
        session_id = validate_correlation_field("session_id", session_id)
        task_id = validate_correlation_field("task_id", task_id)
    name = build_client_request_name(session_id, task_id, api_request_id)
    return str(uuid.uuid5(K_MANUS_RESPONSE_NAMESPACE, name))


# Golden vectors from the frozen plan (byte-match required).
GOLDEN_VECTOR_1_NAME = (
    "k-manus-response-v1|S00000007:sess-qa;S00000006:task-1;S0000000c:turn-1:api:0;"
)
GOLDEN_VECTOR_1_UUID = "ce14c64c-5f4b-562e-9e99-26f03cfbc899"
GOLDEN_VECTOR_2_NAME = "k-manus-response-v1|N;N;S0000000c:turn-1:api:1;"
GOLDEN_VECTOR_2_UUID = "b1e929dd-c4bb-56af-aea2-56167eed5b48"


def apply_managed_correlation_headers(
    api_kwargs: Dict[str, Any],
    *,
    session_id: Optional[str],
    task_id: Optional[str],
    api_request_id: Optional[str],
) -> Dict[str, Any]:
    """Inject Idempotency-Key + correlation headers for managed single-dispatch."""
    client_req = derive_client_request_id(session_id, task_id, api_request_id)
    existing = api_kwargs.get("extra_headers")
    headers: Dict[str, str] = {}
    if isinstance(existing, dict):
        headers.update({str(k): str(v) for k, v in existing.items() if k and v is not None})
    headers[IDEMPOTENCY_HEADER] = client_req
    headers[CLIENT_REQUEST_HEADER] = client_req
    if session_id is not None:
        headers[SESSION_HEADER] = session_id
    if task_id is not None:
        headers[TASK_HEADER] = task_id
    api_kwargs = dict(api_kwargs)
    api_kwargs["extra_headers"] = headers
    return api_kwargs


def normalize_managed_codex_kwargs(api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize codex kwargs for the managed proxy transport contract.

    * ``include``: omitted / ``['reasoning.encrypted_content']``; empty list dropped
    * ``service_tier``: omitted or ``auto`` only (anything else stripped)
    * ``max_tokens`` body field is never emitted; ``max_output_tokens`` keeps 256
    """
    out = dict(api_kwargs)
    include = out.get("include")
    if include == [] or include is None:
        out.pop("include", None)
    elif include is not None:
        # Keep only the managed encrypted-reasoning include token.
        if include != ["reasoning.encrypted_content"]:
            if isinstance(include, list) and "reasoning.encrypted_content" in include:
                out["include"] = ["reasoning.encrypted_content"]
            else:
                out.pop("include", None)
    tier = out.get("service_tier")
    if tier is not None and tier != "auto":
        out.pop("service_tier", None)
    # Wire contract: max_output_tokens, never max_tokens on the body.
    if "max_tokens" in out and "max_output_tokens" not in out:
        out["max_output_tokens"] = out.pop("max_tokens")
    else:
        out.pop("max_tokens", None)
    if out.get("max_output_tokens") is None and out.get("_managed_force_max_output"):
        out["max_output_tokens"] = 256
    return out


def should_block_same_id_recovery(agent: Any, recovery_reason: str) -> bool:
    """True when a recovery must surface the original error instead of retrying."""
    if not bool(getattr(agent, "single_dispatch_mode", False)):
        return False
    # Every named recovery that would re-issue for the same api_request_id
    # is blocked. Unknown reasons are also blocked (fail closed).
    return True


def assert_recovery_table_exhaustive(known_reasons: Iterable[str]) -> None:
    """Test helper: every callable recovery reason is listed in the table."""
    missing = set(known_reasons) - SAME_ID_RECOVERY_REASONS
    if missing:
        raise AssertionError(
            f"single_dispatch recovery table missing reasons: {sorted(missing)}"
        )


def single_dispatch_failed_result(
    *,
    messages: Sequence[Any],
    api_call_count: int,
    api_error: BaseException,
    recovery_reason: str = "unknown_classified_recovery",
) -> Dict[str, Any]:
    """Build the terminal fail payload used when same-id recovery is blocked."""
    summary = str(api_error)
    return {
        "final_response": (
            f"Managed single-dispatch mode blocked recovery "
            f"({recovery_reason}): {summary}"
        ),
        "messages": list(messages),
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": summary,
        "single_dispatch_blocked_recovery": recovery_reason,
    }
