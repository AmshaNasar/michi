"""Agent-loop mechanics, exercised against a stubbed Anthropic client.

The point of these is the confirmation gate: a write tool must never reach its
handler without an explicit yes, and the model must be told plainly when the
user declines.
"""

import sys
import types
from typing import Any, Dict, List

from twin.agent import Agent
from twin.tools.registry import REGISTRY, Tool


# --- stubs ----------------------------------------------------------------

class Block:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)

    def model_dump(self):
        return {"type": self.type, **{k: v for k, v in self.__dict__.items() if k != "type"}}


class Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class StubMessages:
    def __init__(self, scripted: List[Response]):
        self.scripted = scripted
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


class StubClient:
    def __init__(self, scripted):
        self.messages = StubMessages(scripted)


def make_agent(scripted, confirm):
    agent = Agent.__new__(Agent)  # bypass __init__ so no API key is needed
    agent.client = StubClient(scripted)
    agent.model = "stub"
    agent.max_tokens = 512
    agent.confirm = confirm
    agent.messages = []
    return agent


# --- fixtures -------------------------------------------------------------

CALLS: List[Dict[str, Any]] = []


def _register_probe(name, write):
    if REGISTRY.get(name):
        return
    REGISTRY.register(
        Tool(
            name=name,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: CALLS.append({"name": name, "args": args}) or "did it",
            write=write,
        )
    )


_register_probe("probe_read", False)
_register_probe("probe_write", True)


def scripted_tool_call(tool_name):
    return [
        Response(
            [Block("tool_use", id="tu_1", name=tool_name, input={"x": 1})],
            "tool_use",
        ),
        Response([Block("text", text="done")], "end_turn"),
    ]


# --- tests ----------------------------------------------------------------

def test_write_tool_blocked_when_confirmation_denied(monkeypatch):
    CALLS.clear()
    _silence_store(monkeypatch)

    agent = make_agent(scripted_tool_call("probe_write"), confirm=lambda tool, args: False)
    agent.send("do the thing")

    assert CALLS == [], "write handler ran despite the user declining"

    # The model must be told it was declined, not that it errored.
    tool_result = agent.messages[2]["content"][0]
    assert "declined" in tool_result["content"].lower()
    assert "not performed" in tool_result["content"].lower()


def test_write_tool_runs_when_confirmed(monkeypatch):
    CALLS.clear()
    _silence_store(monkeypatch)

    agent = make_agent(scripted_tool_call("probe_write"), confirm=lambda tool, args: True)
    agent.send("do the thing")

    assert [c["name"] for c in CALLS] == ["probe_write"]


def test_read_tool_never_prompts(monkeypatch):
    CALLS.clear()
    _silence_store(monkeypatch)

    prompted = []

    def confirm(tool, args):
        prompted.append(tool.name)
        return False

    agent = make_agent(scripted_tool_call("probe_read"), confirm=confirm)
    agent.send("look something up")

    assert prompted == [], "read tool was gated on confirmation"
    assert [c["name"] for c in CALLS] == ["probe_read"]


def test_default_gate_denies_writes(monkeypatch):
    """A caller that never wired up a confirmation UI must not send anything.

    Constructs a real Agent (so the default gate is the one under test) with
    the API key and SDK client stubbed out.
    """
    CALLS.clear()
    _silence_store(monkeypatch)

    import twin.agent as agent_module

    monkeypatch.setattr(agent_module.SETTINGS, "anthropic_api_key", "stub-key")
    monkeypatch.setattr(
        agent_module.anthropic,
        "Anthropic",
        lambda **kwargs: StubClient(scripted_tool_call("probe_write")),
    )

    agent = Agent()  # no confirm argument -- default gate applies
    agent.send("do the thing")

    assert CALLS == [], "default gate allowed a write tool to run"


def test_handler_exception_is_reported_not_raised(monkeypatch):
    _silence_store(monkeypatch)

    def boom(args):
        raise ValueError("kaboom")

    if not REGISTRY.get("probe_boom"):
        REGISTRY.register(
            Tool(
                name="probe_boom",
                description="probe",
                input_schema={"type": "object", "properties": {}},
                handler=boom,
                write=False,
            )
        )

    agent = make_agent(scripted_tool_call("probe_boom"), confirm=lambda t, a: True)
    agent.send("break it")

    tool_result = agent.messages[2]["content"][0]
    assert "ValueError" in tool_result["content"]
    assert "kaboom" in tool_result["content"]


# --- helpers --------------------------------------------------------------

def _silence_store(monkeypatch):
    """Stub the database so loop tests don't need Postgres."""
    import twin.agent as agent_module

    monkeypatch.setattr(agent_module.store, "get_profile", lambda: {
        "facts": {}, "interests": [], "priorities": {}, "communication_style": "",
        "connected_apps": [], "staleness_threshold_days": 14,
    })
    monkeypatch.setattr(agent_module.store, "pending_nudges", lambda: [])
    monkeypatch.setattr(agent_module.store, "append_turn", lambda role, content: None)
    monkeypatch.setattr(agent_module.store, "mark_nudges_delivered", lambda ids: None)
    monkeypatch.setattr(agent_module.store, "list_open_deadlines", lambda within_days=None: [])
    monkeypatch.setattr(agent_module.store, "stale_projects", lambda *a, **k: [])
