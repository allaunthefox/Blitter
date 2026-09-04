#!/usr/bin/env python3
"""Closed, versioned registry of computation semantic profile identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

U192_ADD_MOD_PROFILE_ID = "BLITTER-ISA-V1"
U192_ADD_MOD_PROFILE_DIGEST = (
    "sha256:cd60e19d6c3652e08d4ed36e3f8208de7f5d2c553cb7e8f5123175c63ff2f129"
)
U192_ADD_MOD_PROFILE_DIGEST_DOMAIN = "declared-field:mathpunch.blitter-isa-profile.v1"
TERNARY_STEP_I32_PROFILE_ID = "blitter-ternary-step-i32-v1"
TERNARY_STEP_I32_PROFILE_DIGEST = (
    "sha256:632c04b77a1fad6349933e682c33229b327ea8d9eb9beb2d265eb7e2a69168e8"
)
TERNARY_STEP_I32_PROFILE_DIGEST_DOMAIN = (
    "canonical-bytes:mathpunch.blitter-semantic-profile.v1"
)
TERNARY_STEP_I32_SOURCE_ARCHITECTURE_ID = "bootstrap-braid-subleq-step-v0"
TERNARY_STEP_I32_SOURCE_ARCHITECTURE_DIGEST = (
    "sha256:f10008de0f9d81c96bbb01d181ad3802081173fe7808a9ac9c6772eb789ae7fb"
)

TERMINAL_CONTRACT_DIGEST_DOMAIN = (
    "canonical-bytes:mathpunch.blitter-terminal-contract.v1"
)
U192_TERMINAL_CONTRACT_DIGEST = (
    "sha256:5a9804978698791d50d937ba93b7f279344b2e0ea658f4e2ee7b2d5a3365fb7f"
)
TERNARY_STEP_I32_TERMINAL_CONTRACT_DIGEST = (
    "sha256:ac5a1e4b691fd89abb3459e0dad132cd0a44f0c5252f892b6a1ffb58d0337c21"
)


class SemanticProfileError(ValueError):
    """A semantic profile ID or digest is not admitted by this registry."""


@dataclass(frozen=True)
class TerminalStatus:
    code: int | None
    name: str
    state_effect: str


@dataclass(frozen=True)
class TerminalContract:
    contract_id: str
    digest: str
    digest_domain: str
    statuses: tuple[TerminalStatus, ...]
    malformed_request: str
    authority: str


@dataclass(frozen=True)
class SemanticProfile:
    profile_id: str
    version: int
    semantic_digest: str
    semantic_digest_domain: str
    operation: str
    request_codec: str
    result_codec: str
    terminal_contract: TerminalContract
    source_architecture_id: str | None
    source_architecture_digest: str | None


_PROFILES = {
    U192_ADD_MOD_PROFILE_ID: SemanticProfile(
        profile_id=U192_ADD_MOD_PROFILE_ID,
        version=1,
        semantic_digest=U192_ADD_MOD_PROFILE_DIGEST,
        semantic_digest_domain=U192_ADD_MOD_PROFILE_DIGEST_DOMAIN,
        operation="u192.add-mod",
        request_codec="mathpunch.u192-abi.v1",
        result_codec="mathpunch.u192-canonical.v1",
        terminal_contract=TerminalContract(
            contract_id="mathpunch.blitter-computation-result-terminal.v1",
            digest=U192_TERMINAL_CONTRACT_DIGEST,
            digest_domain=TERMINAL_CONTRACT_DIGEST_DOMAIN,
            statuses=(TerminalStatus(None, "accepted", "canonical-result"),),
            malformed_request="reject-no-result",
            authority="none",
        ),
        source_architecture_id=None,
        source_architecture_digest=None,
    ),
    TERNARY_STEP_I32_PROFILE_ID: SemanticProfile(
        profile_id=TERNARY_STEP_I32_PROFILE_ID,
        version=1,
        semantic_digest=TERNARY_STEP_I32_PROFILE_DIGEST,
        semantic_digest_domain=TERNARY_STEP_I32_PROFILE_DIGEST_DOMAIN,
        operation="blitter.ternary-step-i32",
        request_codec="mathpunch.blitter-ternary-step-i32-request.v1",
        result_codec="mathpunch.blitter-ternary-step-i32-result.v1",
        terminal_contract=TerminalContract(
            contract_id="mathpunch.blitter-ternary-step-i32-terminal.v1",
            digest=TERNARY_STEP_I32_TERMINAL_CONTRACT_DIGEST,
            digest_domain=TERMINAL_CONTRACT_DIGEST_DOMAIN,
            statuses=(
                TerminalStatus(0, "accepted", "next-state"),
                TerminalStatus(1, "write-conflict", "input-state"),
                TerminalStatus(2, "domain-exceeded", "input-state"),
            ),
            malformed_request="reject-no-result",
            authority="none",
        ),
        source_architecture_id=TERNARY_STEP_I32_SOURCE_ARCHITECTURE_ID,
        source_architecture_digest=TERNARY_STEP_I32_SOURCE_ARCHITECTURE_DIGEST,
    ),
}

PROFILES: Mapping[str, SemanticProfile] = MappingProxyType(_PROFILES)


def known_profile_ids() -> tuple[str, ...]:
    return tuple(PROFILES)


def resolve_profile(profile_id: str, semantic_digest: str) -> SemanticProfile:
    """Resolve an exact ID/digest pair; unknown or stale bindings fail closed."""
    if not isinstance(profile_id, str) or not profile_id:
        raise SemanticProfileError("profile_id must be a non-empty string")
    if not isinstance(semantic_digest, str) or SHA256_RE.fullmatch(semantic_digest) is None:
        raise SemanticProfileError("semantic_digest must be sha256:<64 lowercase hex>")
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise SemanticProfileError(f"unknown semantic profile: {profile_id}") from exc
    if semantic_digest != profile.semantic_digest:
        raise SemanticProfileError(f"semantic digest mismatch for profile: {profile_id}")
    return profile
