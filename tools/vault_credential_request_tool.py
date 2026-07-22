#!/usr/bin/env python3
"""
Vault Credential Request Tool — tool-driven inline credential capture.

Lets the agent ask the Dolshoi desktop shell to collect a missing vault
login (e.g. an SRT account) mid-turn. The tool never sees credential
values: the platform callback renders the host UI, the user types into the
app, and the host stores the login in the cloud vault and provisions a
credential-action capability. The tool result tells the agent whether the
credential was saved (and, when provisioned, the capability id to retry
with `vault-run`) or skipped.
"""

import json
from typing import Optional, Callable


def request_vault_credential_tool(
    service: str,
    item_name: Optional[str] = None,
    display_name: Optional[str] = None,
    reason: Optional[str] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user to enter a missing vault login for an external service.

    Args:
        service: Registered credential-action service key (e.g. "srt").
        item_name: Vault item name to store under; defaults to `service`.
        display_name: Human-facing service label (e.g. "SRT"); defaults to
                      an uppercased `service`.
        reason: One short sentence on why the login is needed, shown to the
                user in the credential card.
        callback: Platform-provided function that drives the actual UI.
                  Signature:
                      callback(service, item_name, display_name, reason) -> str
                  returning a JSON string with {"status": "saved"|"skipped",
                  "capability": {...}|null}. Injected by the agent runner.

    Returns:
        JSON string describing the outcome for the agent loop.
    """
    if not service or not service.strip():
        return tool_error("service is required.")

    service = service.strip().lower()
    item_name = (item_name or "").strip() or service
    display_name = (display_name or "").strip() or service.upper()
    reason = (reason or "").strip() or None

    if callback is None:
        return json.dumps(
            {
                "status": "unavailable",
                "error": (
                    "Credential capture is not available in this execution "
                    "context. Proceed without the login or ask the user to "
                    "add it in the app's vault settings."
                ),
            },
            ensure_ascii=False,
        )

    try:
        result = callback(service, item_name, display_name, reason)
    except Exception as exc:
        return json.dumps(
            {"status": "error", "error": f"Failed to collect credential: {exc}"},
            ensure_ascii=False,
        )

    return str(result)


def check_vault_credential_requirements() -> bool:
    """The tool degrades gracefully when no callback is wired — always listed."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

VAULT_CREDENTIAL_SCHEMA = {
    "name": "request_vault_credential",
    "description": (
        "Ask the user to enter a missing login for an external service "
        "account (e.g. SRT train booking) so it can be stored in the Dolshoi "
        "vault and used via the credential-action broker (`vault-run`).\n\n"
        "Call this tool when ALL of the following hold:\n"
        "- The task requires acting on a service that needs the user's own "
        "account login (id + password).\n"
        "- No capability for that service is listed in the turn's "
        "'Credential Actions' capabilities note.\n\n"
        "The tool parks until the user either saves the login or skips. On "
        "'saved' the result carries a fresh capability — IMMEDIATELY retry "
        "the intended action with `vault-run <capabilityId> <action> ...`. "
        "On 'skipped', proceed without the login (best effort) instead of "
        "asking for the password in chat.\n\n"
        "NEVER ask the user to type their password into the chat — this "
        "tool is the only sanctioned credential capture path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": (
                    "Registered credential-action service key, lowercase "
                    "(e.g. 'srt'). Must match a service the host broker can "
                    "provision capabilities for."
                ),
            },
            "item_name": {
                "type": "string",
                "description": (
                    "Vault item name to store the login under. Omit to "
                    "default to `service`."
                ),
            },
            "display_name": {
                "type": "string",
                "description": (
                    "Human-facing service label shown on the credential card "
                    "(e.g. 'SRT'). Omit to default to an uppercased `service`."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence explaining why the login is needed, "
                    "shown to the user on the credential card."
                ),
            },
        },
        "required": ["service"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

# Registered under the always-on "clarify" toolset: interactive user
# round-trips share one toolset so desktop turns enable both with the same
# config as the clarify tool.
registry.register(
    name="request_vault_credential",
    toolset="clarify",
    schema=VAULT_CREDENTIAL_SCHEMA,
    handler=lambda args, **kw: request_vault_credential_tool(
        service=args.get("service", ""),
        item_name=args.get("item_name"),
        display_name=args.get("display_name"),
        reason=args.get("reason"),
        callback=kw.get("callback")),
    check_fn=check_vault_credential_requirements,
    emoji="🔐",
)
