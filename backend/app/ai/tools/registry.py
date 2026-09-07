"""Tool registry: what a given agent is allowed to call."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from app.ai.providers.base import ToolSpec
from app.ai.tools.base import Tool


class ToolRegistry:
    """A named set of tools. Agents hold a subset, never the global set."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Look up by name. Returns None for anything the model invented."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def list_specs(self) -> list[ToolSpec]:
        """Advertisements for every registered tool."""
        return [tool.spec() for tool in self._tools.values()]

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """A narrower registry — how a future agent gets fewer tools."""
        return ToolRegistry(
            self._tools[name] for name in names if name in self._tools
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())
