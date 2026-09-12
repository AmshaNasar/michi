"""The agent core: a single tool-calling loop against the Anthropic API.

Deliberately one agent, not an orchestration of several (spec section 5).
Proactive checks run as a scheduled job and hand their results to this loop as
queued nudges rather than acting as independent agents.
"""

import datetime as dt
import json
from typing import Any, Callable, Dict, List, Optional

import anthropic

from twin.config import SETTINGS
from twin.memory import store
from twin.tools import memory_tools  # noqa: F401  (registers memory tools)
from twin.tools.registry import REGISTRY, Tool

# A confirmation gate receives the tool about to fire and its arguments, and
# returns True to allow it. Write tools never run without one.
ConfirmFn = Callable[[Tool, Dict[str, Any]], bool]

MAX_TOOL_ITERATIONS = 12

PERSONA = """\
You are the user's digital twin: a private, inward-facing assistant that keeps \
a persistent model of one person and works on their behalf. You are not a \
neutral utility and not a chatbot that represents them to anyone else.

How you behave:

- Proactive, not passive. If you notice a stalled project, a free evening, an \
approaching deadline, or an unclaimed opportunity, you raise it yourself. You \
do not wait to be asked.
- Assertive and specific. "You've had three open evenings this week and \
haven't touched the guitar project -- want to block an hour tonight?" Never \
"you have a reminder."
- Grounded in what you actually track. Every nudge cites real data: a tracked \
project's last activity, a real calendar gap, a stated interest, a recorded \
deadline. If you have no evidence, say so plainly instead of inventing a \
plausible-sounding suggestion.
- Never assume a life stage. Do not reason about what "a student" or "an \
employee" typically wants. Every suggestion derives from this user's own \
tracked data. If you don't know something about them, look it up with recall \
or get_profile, or ask.

Memory discipline: when the user reveals a durable preference, pattern, or \
commitment, store it with remember_fact. When they mention something they are \
working on, register it with track_project. Do this as you go, without \
announcing it.

Safety: any action that sends or publishes something to another person \
requires the user's explicit confirmation first. You will be asked to confirm \
before such a tool runs -- never imply an outbound action has happened when it \
has not.
"""


def build_system_prompt(
    profile: Dict[str, Any],
    nudges: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Assemble the system prompt: persona + the current state of the twin."""
    now = dt.datetime.now(dt.timezone.utc)

    sections = [PERSONA, "## Current context\n"]
    sections.append("Current time (UTC): {0}".format(now.isoformat(timespec="seconds")))

    facts = profile.get("facts") or {}
    if facts:
        rendered = "\n".join(
            "- {0}: {1}".format(key, entry.get("value"))
            for key, entry in facts.items()
        )
        sections.append("Known facts about the user:\n{0}".format(rendered))

    interests = profile.get("interests") or []
    if interests:
        sections.append("Stated interests: {0}".format(", ".join(interests)))

    priorities = profile.get("priorities") or {}
    if priorities:
        sections.append(
            "Priority weighting from onboarding (higher = they care more):\n{0}".format(
                json.dumps(priorities, indent=2)
            )
        )

    if profile.get("communication_style"):
        sections.append("Communication style: {0}".format(profile["communication_style"]))

    connected = profile.get("connected_apps") or []
    sections.append(
        "Connected integrations: {0}".format(", ".join(connected) if connected else "none")
    )

    # Surface the scheduler's findings so the agent can lead with them.
    deadlines = store.list_open_deadlines(within_days=14)
    if deadlines:
        sections.append(
            "Open deadlines in the next 14 days:\n{0}".format(
                "\n".join(
                    "- #{0} {1} (due {2})".format(
                        d["id"], d["description"], d["due_at"].isoformat(timespec="minutes")
                    )
                    for d in deadlines
                )
            )
        )

    stale = store.stale_projects()
    if stale:
        sections.append(
            "Projects past the user's staleness threshold of {0} days:\n{1}".format(
                profile["staleness_threshold_days"],
                "\n".join(
                    "- {0} ({1} days since last activity)".format(p["name"], p["days_stale"])
                    for p in stale
                ),
            )
        )

    if nudges:
        sections.append(
            "Queued nudges to raise with the user now, in your own voice:\n{0}".format(
                "\n".join("- {0}".format(n["message"]) for n in nudges)
            )
        )

    return "\n\n".join(sections)


def _serialize(blocks: Any) -> Any:
    """Convert SDK content blocks into JSON-safe structures for storage."""
    if isinstance(blocks, list):
        return [_serialize(block) for block in blocks]
    if hasattr(blocks, "model_dump"):
        return blocks.model_dump()
    return blocks


class Agent:
    def __init__(self, confirm: Optional[ConfirmFn] = None, max_tokens: int = 2048):
        SETTINGS.require_agent_key()
        client_kwargs = {"api_key": SETTINGS.anthropic_api_key}
        if SETTINGS.agent_base_url:
            # Points the same Messages API at a compatible gateway (e.g.
            # OpenRouter). Tool-use blocks pass through unchanged.
            client_kwargs["base_url"] = SETTINGS.agent_base_url
        self.client = anthropic.Anthropic(**client_kwargs)
        self.model = SETTINGS.agent_model
        self.max_tokens = max_tokens
        # Default gate denies writes: a caller that hasn't wired up a
        # confirmation UI should not be able to send mail by accident.
        self.confirm = confirm if confirm is not None else (lambda tool, args: False)
        self.messages: List[Dict[str, Any]] = []

    def load_history(self, limit: int = 20) -> None:
        """Replay recent turns so a new session resumes where the last left off."""
        self.messages = [
            {"role": turn["role"], "content": turn["content"]}
            for turn in store.recent_turns(limit)
        ]

    def _tool_definitions(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        connected = list(profile.get("connected_apps") or [])
        return [tool.to_anthropic() for tool in REGISTRY.available(connected)]

    def _run_tool(self, name: str, args: Dict[str, Any]) -> str:
        tool = REGISTRY.get(name)
        if tool is None:
            return "Error: no such tool '{0}'.".format(name)

        if tool.write and not self.confirm(tool, args):
            # Reported back to the model as a normal result so it can adapt,
            # rather than as an error it might retry around.
            return (
                "The user declined this action. It was NOT performed. "
                "Do not retry it; ask what they'd like changed instead."
            )

        try:
            return tool.handler(args)
        except Exception as exc:  # surfaced to the model, not swallowed
            return "Tool '{0}' failed: {1}: {2}".format(
                name, type(exc).__name__, exc
            )

    def send(self, user_message: str) -> str:
        """Run one user turn to completion, including any tool calls."""
        profile = store.get_profile()
        nudges = store.pending_nudges()
        system_prompt = build_system_prompt(profile, nudges)
        tools = self._tool_definitions(profile)

        self.messages.append({"role": "user", "content": user_message})
        store.append_turn("user", user_message)

        final_text = ""
        for _ in range(MAX_TOOL_ITERATIONS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                tools=tools,
                messages=self.messages,
            )

            assistant_content = _serialize(response.content)
            self.messages.append({"role": "assistant", "content": assistant_content})
            store.append_turn("assistant", assistant_content)

            text_blocks = [
                block.text for block in response.content if block.type == "text"
            ]
            if text_blocks:
                final_text = "\n".join(text_blocks)

            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                output = self._run_tool(block.name, dict(block.input or {}))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )

            self.messages.append({"role": "user", "content": results})
            store.append_turn("user", results)
        else:
            final_text = final_text or (
                "I hit the tool-call limit for this turn without finishing. "
                "Ask me to continue and I'll pick up from here."
            )

        # Nudges are only marked delivered once they've actually been in front
        # of the model, so a crashed turn doesn't silently lose them.
        store.mark_nudges_delivered([n["id"] for n in nudges])
        return final_text
