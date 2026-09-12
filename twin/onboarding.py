"""First-run onboarding, also reachable later as settings.

Nothing connects without an explicit opt-in here, and the questionnaire seeds
the priority weighting so the agent's proactivity is tuned from day one rather
than guessed at (spec section 4).
"""

import datetime as dt
from typing import Any, Dict, List

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from twin import credentials as credstore
from twin.memory import store
from twin.timeutil import detect_timezone, zone
from twin.tools import google_auth

console = Console()

# Tier 1 is connectable today. Tier 1b entries are listed so the user can see
# the roadmap, but are not offered until their adapters exist.
INTEGRATIONS = [
    {"key": "gmail", "label": "Gmail", "tier": 1, "available": True, "provider": "google"},
    {"key": "calendar", "label": "Google Calendar", "tier": 1, "available": True, "provider": "google"},
    {"key": "slack", "label": "Slack", "tier": "1b", "available": True, "provider": "slack"},
    {"key": "notion", "label": "Notion", "tier": "1b", "available": True, "provider": "notion"},
    {"key": "todoist", "label": "Todoist", "tier": "1b", "available": True, "provider": "todoist"},
]

PRIORITY_OPTIONS = [
    ("email_triage", "Triaging and summarizing my email"),
    ("deadline_tracking", "Tracking deadlines and action items"),
    ("project_accountability", "Keeping me honest about personal projects"),
    ("opportunity_discovery", "Finding events and opportunities I'd want"),
]

DEFAULT_STALENESS_DAYS = 14


def _pick_priorities() -> Dict[str, float]:
    """Rank the four capability areas into a weighting the agent can act on."""
    console.print()
    console.print(
        Panel(
            "What do you most want help with? Pick the ones that matter, in "
            "order of importance.",
            title="Priorities",
            border_style="cyan",
        )
    )
    table = Table(show_header=False, box=None, pad_edge=False)
    for index, (_, label) in enumerate(PRIORITY_OPTIONS, start=1):
        table.add_row("  [bold cyan]{0}[/]".format(index), label)
    console.print(table)
    console.print()

    raw = Prompt.ask(
        "  Enter numbers, most important first (e.g. [dim]3,1,2[/])",
        default="1,2,3,4",
    )

    chosen: List[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        index = int(chunk) - 1
        if 0 <= index < len(PRIORITY_OPTIONS):
            key = PRIORITY_OPTIONS[index][0]
            if key not in chosen:
                chosen.append(key)

    if not chosen:
        chosen = [key for key, _ in PRIORITY_OPTIONS]

    # Linear decay by rank; unpicked areas stay on at low weight so the agent
    # still notices them without leading with them.
    priorities = {key: 0.15 for key, _ in PRIORITY_OPTIONS}
    for rank, key in enumerate(chosen):
        priorities[key] = round(1.0 - (rank * 0.8 / max(len(chosen), 1)), 2)
    return priorities


def _pick_interests() -> List[str]:
    """Collect concrete interest tags up front.

    Spec section 14 left this open. Collecting it here means opportunity
    matching has something real to work from on day one instead of waiting
    weeks to infer it.
    """
    console.print()
    console.print(
        Panel(
            "What are you actually into? Specific beats broad -- 'algorithmic "
            "trading' and 'fingerstyle guitar' give better matches than 'tech' "
            "and 'music'.",
            title="Interests",
            border_style="cyan",
        )
    )
    raw = Prompt.ask("  Comma-separated", default="")
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _connect_apps() -> List[str]:
    console.print()
    console.print(
        Panel(
            "Pick what to connect. Nothing is accessed unless you enable it "
            "here, and you can change this any time.",
            title="Connections",
            border_style="cyan",
        )
    )

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("#")
    table.add_column("App")
    table.add_column("Tier")
    table.add_column("Status")
    for index, integration in enumerate(INTEGRATIONS, start=1):
        if not integration["available"]:
            status = "[dim]not built yet[/]"
        elif credstore.is_connected(integration["key"]):
            status = "[green]connected[/]"
        else:
            status = "[yellow]not connected[/]"
        table.add_row(
            str(index) if integration["available"] else "[dim]-[/]",
            integration["label"],
            str(integration["tier"]),
            status,
        )
    console.print(table)
    console.print()

    selectable = [i for i in INTEGRATIONS if i["available"]]
    raw = Prompt.ask(
        "  Numbers to connect (blank to skip)",
        default="",
    )

    connected = [i["key"] for i in INTEGRATIONS if credstore.is_connected(i["key"])]

    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        index = int(chunk) - 1
        if not (0 <= index < len(INTEGRATIONS)):
            continue
        integration = INTEGRATIONS[index]
        if not integration["available"]:
            continue
        if integration["key"] in connected:
            continue

        try:
            if integration["provider"] == "google":
                console.print(
                    "\n  Opening your browser to authorize [bold]{0}[/]...".format(
                        integration["label"]
                    )
                )
                google_auth.connect(integration["key"])
                console.print("  [green]{0} connected.[/]".format(integration["label"]))
            elif integration["provider"] == "slack":
                _connect_slack()
            elif integration["provider"] == "notion":
                _connect_notion()
            elif integration["provider"] == "todoist":
                _connect_todoist()
            else:
                console.print(
                    "  [dim]{0} has no adapter yet.[/]".format(integration["label"])
                )
                continue

            if credstore.is_connected(integration["key"]):
                connected.append(integration["key"])
        except FileNotFoundError as exc:
            console.print("  [yellow]Skipped {0}.[/]".format(integration["label"]))
            console.print("  [dim]{0}[/]".format(exc))
        except Exception as exc:
            console.print(
                "  [red]Could not connect {0}: {1}[/]".format(integration["label"], exc)
            )

    return connected


def _connect_slack() -> None:
    """Collect Slack tokens.

    Slack's OAuth flow needs an HTTPS redirect URL, which a local-only app has
    nowhere to host -- so the user creates their own app and pastes the tokens.
    Only the bot token is required.
    """
    from twin.tools import slack_auth

    console.print()
    console.print(
        Panel(
            "Create a Slack app at [bold]api.slack.com/apps[/] (From scratch), then:\n\n"
            "1. [bold]OAuth & Permissions[/] → add these Bot Token Scopes:\n"
            "   [dim]{0}[/]\n"
            "2. Install to your workspace, and copy the [bold]Bot User OAuth Token[/] "
            "([dim]xoxb-…[/])\n"
            "3. Optional — [bold]Basic Information → App-Level Tokens[/]: create one with "
            "[dim]connections:write[/] ([dim]xapp-…[/]) to enable the live listener\n"
            "4. Optional — a [bold]User OAuth Token[/] ([dim]xoxp-…[/]) with "
            "[dim]search:read[/] enables Slack search\n\n"
            "Tokens are stored in your OS keychain, never on disk.".format(
                ", ".join(slack_auth.BOT_SCOPES)
            ),
            title="Connecting Slack",
            border_style="cyan",
        )
    )

    bot_token = Prompt.ask("  Bot token (xoxb-)", password=True, default="").strip()
    if not bot_token:
        console.print("  [yellow]Skipped Slack.[/]")
        return

    try:
        identity = slack_auth.validate_bot_token(bot_token)
    except slack_auth.SlackError as exc:
        console.print("  [red]{0}[/]".format(exc))
        return

    console.print(
        "  [green]Connected to '{0}' as {1}.[/]".format(
            identity["team"], identity["bot_name"]
        )
    )

    app_tok = Prompt.ask(
        "  App-level token (xapp-) [dim]optional, enables live listener[/]",
        password=True,
        default="",
    ).strip()
    user_tok = Prompt.ask(
        "  User token (xoxp-) [dim]optional, enables search[/]",
        password=True,
        default="",
    ).strip()

    if user_tok:
        # auth.test on the user token tells us who the human is, which lets the
        # listener tell their commitments apart from everyone else's.
        try:
            human = slack_auth.validate_bot_token(user_tok)
            identity["human_user_id"] = human.get("bot_user_id", "")
        except slack_auth.SlackError as exc:
            console.print("  [yellow]User token rejected ({0}) -- skipping search.[/]".format(exc))
            user_tok = ""

    slack_auth.save_tokens(bot_token, app_tok or None, user_tok or None, identity)
    extras = []
    if app_tok:
        extras.append("listener")
    if user_tok:
        extras.append("search")
    if extras:
        console.print("  [green]Enabled: {0}.[/]".format(", ".join(extras)))


def _connect_notion() -> None:
    """Collect a Notion internal-integration token.

    Same reasoning as Slack: Notion's OAuth flow needs a hosted redirect URL.
    """
    from twin.tools import notion_auth

    console.print()
    console.print(
        Panel(
            "Create an integration at [bold]notion.so/my-integrations[/] "
            "(New integration → Internal), then copy its "
            "[bold]Internal Integration Secret[/].\n\n"
            "[yellow]Then share your pages with it[/] — a Notion integration "
            "sees [bold]nothing[/] by default. In Notion, open the page or "
            "database, click [bold]...[/] → [bold]Connections[/] → add your "
            "integration. Sharing a parent page shares everything under it.\n\n"
            "The token is stored in your OS keychain, never on disk.",
            title="Connecting Notion",
            border_style="cyan",
        )
    )

    token = Prompt.ask("  Integration secret", password=True, default="").strip()
    if not token:
        console.print("  [yellow]Skipped Notion.[/]")
        return

    try:
        identity = notion_auth.validate_token(token)
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        console.print("  [red]{0}[/]".format(exc))
        return

    notion_auth.save_token(token, identity)
    workspace = identity.get("workspace_name") or "your workspace"
    console.print("  [green]Connected to {0} as '{1}'.[/]".format(workspace, identity["name"]))


def _connect_todoist() -> None:
    """Collect a Todoist personal API token."""
    from twin.tools import todoist_auth

    console.print()
    console.print(
        Panel(
            "In Todoist, go to [bold]Settings → Integrations → Developer[/] and "
            "copy your [bold]API token[/].\n\n"
            "It's stored in your OS keychain, never on disk.",
            title="Connecting Todoist",
            border_style="cyan",
        )
    )

    token = Prompt.ask("  API token", password=True, default="").strip()
    if not token:
        console.print("  [yellow]Skipped Todoist.[/]")
        return

    try:
        todoist_auth.validate_token(token)
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        console.print("  [red]{0}[/]".format(exc))
        return

    todoist_auth.save_token(token)
    console.print("  [green]Todoist connected.[/]")


def _pick_availability(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Timezone and active hours.

    Free-time detection is meaningless without these, and they must be the
    user's own answer -- a hardcoded 9-to-5 would be exactly the life-stage
    assumption the spec rules out.
    """
    console.print()
    console.print(
        Panel(
            "When are you actually available? I use this to spot real openings "
            "in your calendar instead of guessing.",
            title="Availability",
            border_style="cyan",
        )
    )

    current_tz = profile.get("timezone") or ""
    suggested = current_tz if current_tz and current_tz != "UTC" else detect_timezone()
    timezone_name = Prompt.ask("  Your timezone", default=suggested)
    if zone(timezone_name) is dt.timezone.utc and timezone_name != "UTC":
        console.print(
            "  [yellow]Didn't recognise '{0}' -- falling back to UTC. "
            "Re-run onboarding to fix.[/]".format(timezone_name)
        )
        timezone_name = "UTC"

    hours = profile.get("active_hours") or {}
    start = IntPrompt.ask("  Earliest hour you'd work (0-23)", default=int(hours.get("start", 9)))
    end = IntPrompt.ask("  Latest hour (0-24)", default=int(hours.get("end", 22)))

    start = max(0, min(start, 23))
    end = max(0, min(end, 24))
    if end <= start:
        console.print("  [yellow]End must be after start -- using {0}:00-24:00.[/]".format(start))
        end = 24

    # Anchors "near me" when searching for opportunities. Optional -- without
    # it searches simply aren't location-filtered.
    location = Prompt.ask(
        "  Where are you based? [dim](city, country -- used to find local things)[/]",
        default=profile.get("location") or "",
    )

    return {
        "timezone": timezone_name,
        "active_hours": {"start": start, "end": end},
        "location": location.strip(),
    }


def _pick_staleness_threshold(current: int) -> int:
    console.print()
    console.print(
        Panel(
            "How long should a project sit untouched before I bring it up?",
            title="Project accountability",
            border_style="cyan",
        )
    )
    return IntPrompt.ask("  Days", default=current or DEFAULT_STALENESS_DAYS)


def run_onboarding() -> Dict[str, Any]:
    profile = store.get_profile()

    console.print()
    console.print(
        Panel(
            "I build a persistent model of how you work -- your projects, "
            "deadlines, habits, and interests -- and use it to stay ahead of "
            "you rather than wait to be asked.\n\n"
            "This takes about a minute. Everything stays on this machine.",
            title="[bold]Setting up your digital twin[/]",
            border_style="bold cyan",
        )
    )

    connected = _connect_apps()
    priorities = _pick_priorities()
    interests = _pick_interests()
    availability = _pick_availability(profile)
    threshold = _pick_staleness_threshold(profile["staleness_threshold_days"])

    console.print()
    style = Prompt.ask(
        "  Anything about how you want me to talk to you? (blank for default)",
        default="",
    )

    updated = store.update_profile(
        connected_apps=connected,
        priorities=priorities,
        staleness_threshold_days=threshold,
        onboarded_at=dt.datetime.now(dt.timezone.utc),
        timezone=availability["timezone"],
        active_hours=availability["active_hours"],
        location=availability["location"],
        **({"communication_style": style} if style else {})
    )
    if interests:
        store.add_interests(interests)
        updated = store.get_profile()

    console.print()
    console.print(
        Panel(
            "Connected: {0}\nTop priority: {1}\nInterests: {2}\n"
            "Available: {3:02d}:00-{4:02d}:00 ({5})\nBased in: {6}\n"
            "Nudge threshold: {7} days\n\n"
            "Run [bold]python -m twin.cli chat[/] to start.".format(
                ", ".join(connected) or "nothing yet",
                max(priorities, key=priorities.get),
                ", ".join(updated["interests"]) or "none yet",
                availability["active_hours"]["start"],
                availability["active_hours"]["end"],
                availability["timezone"],
                availability["location"] or "not set",
                threshold,
            ),
            title="[bold green]Ready[/]",
            border_style="green",
        )
    )
    return updated


def needs_onboarding() -> bool:
    return store.get_profile()["onboarded_at"] is None
