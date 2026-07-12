"""Fault-injection resilience harness for the RentCompass agent.

This package injects faults at the REAL mockable seams of the production stack
(``core.tool_system.Tool``/``ToolRegistry``, ``core.mcp_client.MCPToolClient``,
``uk_rent_agent.tools.idempotency.IdempotencyStore``,
``uk_rent_agent.agent.guardrails.sanitize_untrusted``, the LangGraph
``search_worker``/``gather_searches`` map-reduce nodes, the ``generate_response``
and ``critic`` nodes, and ``uk_rent_agent.agent.critic.enforce_grounding``) and
measures how the agent behaves under each fault.

Everything runs OFFLINE and UNBILLED: the model is faked (or made to raise on
purpose), but the tool/graph/idempotency/guardrail code exercised is the real
production code — so the resilience numbers this harness reports are GENUINE, not
fabricated.

Entry point::

    python -m evaluation.fault_injection.run

writes ``evaluation/results/fault_injection.csv`` (per scenario) and
``evaluation/results/fault_summary.json`` (aggregate).
"""
from .injectors import ScenarioResult  # noqa: F401
