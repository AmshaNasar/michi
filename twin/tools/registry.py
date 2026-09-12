"""Tool registry.

Each integration contributes a handful of actions. Tools are shaped close to
an MCP server interface -- a name, a JSON schema, and a handler taking a dict
and returning a string -- so they can be lifted out into real MCP servers
later without touching the agent loop.

Every tool declares whether it *writes*. Write tools never fire without
explicit user confirmation (spec section 11), and the split is enforced here
rather than being left to the model's judgement.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[Dict[str, Any]], str]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Handler
    # Write tools mutate the outside world (send mail, create events) and are
    # gated on confirmation. Tools that only mutate local memory are reads as
    # far as the gate is concerned -- they are the agent learning, not acting.
    write: bool = False
    # Which integration must be connected for this tool to be offered. None
    # means always available (local memory tools).
    requires: Optional[str] = None

    def to_anthropic(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class Registry:
    tools: Dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError("Duplicate tool name: {0}".format(tool.name))
        self.tools[tool.name] = tool
        return tool

    def available(self, connected_apps: List[str]) -> List[Tool]:
        """Tools whose backing integration the user has actually connected."""
        connected = set(connected_apps)
        return [
            tool
            for tool in self.tools.values()
            if tool.requires is None or tool.requires in connected
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)


REGISTRY = Registry()


def tool(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    write: bool = False,
    requires: Optional[str] = None,
) -> Callable[[Handler], Handler]:
    """Register a function as an agent tool."""

    def decorator(handler: Handler) -> Handler:
        REGISTRY.register(
            Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                write=write,
                requires=requires,
            )
        )
        return handler

    return decorator


def obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    """Shorthand for a JSON-schema object."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }
