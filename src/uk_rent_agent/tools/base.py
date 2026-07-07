from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ToolEnvelope(BaseModel):
    """The sole result shape for in-process and MCP tool execution."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any | None = None
    error: str | None = None
    tool: str
    version: str = "1"
    elapsed_ms: float = 0
    idempotency_key: str | None = None

    def legacy_dict(self) -> dict[str, Any]:
        return {
            "success": self.ok,
            "data": self.data,
            "error": self.error,
            "tool_name": self.tool,
            "version": self.version,
            "execution_time_ms": self.elapsed_ms,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    side_effect: Literal["none", "write"] = "none"
    retry_safe: bool = True
    cacheable: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def validate_input(self, values: dict[str, Any]) -> dict[str, Any]:
        return self.input_model.model_validate(values).model_dump(exclude_none=True)

    def validate_output(self, value: Any) -> Any:
        if self.output_model is None:
            return value
        return self.output_model.model_validate(value).model_dump()
