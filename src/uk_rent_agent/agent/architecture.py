"""Conversational architecture identities and runtime compatibility groups.

Keep these identities separate from the production canary's two-arm vocabulary.
``manager_v1`` is an opt-in development architecture that reuses the ``fc_loop``
runtime; its Phase-2 specialist path has a separate, default-off rollout switch.
It is deliberately not a third production canary arm.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias


AgentArchitecture: TypeAlias = Literal["legacy", "fc_loop", "manager_v1"]

LEGACY_ARCH: Final = "legacy"
FC_LOOP_ARCH: Final = "fc_loop"
MANAGER_V1_ARCH: Final = "manager_v1"

SUPPORTED_AGENT_ARCHES: Final[frozenset[str]] = frozenset(
    {LEGACY_ARCH, FC_LOOP_ARCH, MANAGER_V1_ARCH}
)

# ``manager_v1`` inherits every FC runtime semantic (message/memory assembly,
# model injection and observations). Its specialist dispatcher is controlled by
# a second switch and therefore does not make ``fc_loop`` specialist-capable.
FC_RUNTIME_ARCHES: Final[frozenset[str]] = frozenset(
    {FC_LOOP_ARCH, MANAGER_V1_ARCH}
)


def normalize_agent_arch(value: str | None) -> str:
    """Normalize an architecture value at process/bootstrap boundaries."""
    return str(value or LEGACY_ARCH).strip().lower()


def uses_fc_runtime(value: str | None) -> bool:
    """Whether ``value`` must use the native function-calling runtime semantics."""
    return normalize_agent_arch(value) in FC_RUNTIME_ARCHES


def manager_v1_specialists_enabled(
    agent_arch: str | None,
    requested: bool,
) -> bool:
    """Return the effective Phase-2 rollout state.

    The feature is intentionally architecture-bound: setting its environment
    switch in a legacy or ``fc_loop`` process must never activate specialist
    execution accidentally.
    """
    return normalize_agent_arch(agent_arch) == MANAGER_V1_ARCH and bool(requested)
