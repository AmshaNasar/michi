"""Command-line entrypoint.

    python -m twin.cli onboard    first-run setup / settings
    python -m twin.cli chat       talk to the twin
    python -m twin.cli voice      talk to the twin out loud
    python -m twin.cli status     what the twin currently knows
    python -m twin.cli check      run the proactive checks once
    python -m twin.cli watch      run the proactive checks on a loop
    python -m twin.cli service    run the proactive checks in the background
    python -m twin.cli slack-listen   stream Slack events over Socket Mode
    python -m twin.cli gmail-watch    register/inspect Gmail push
"""

import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from twin import onboarding, scheduler
from twin.agent import Agent
from twin.config import SETTINGS
from twin.memory import store
from twin.memory.db import apply_schema
from twin.tools import (  # noqa: F401  (registers tools)
    calendar_tools,
    gmail_tools,
    notion_tools,
    opportunity_tools,
    slack_tools,
    todoist_tools,
)
from twin.tools.registry import Tool

console = Console()


def confirm_write(tool: Tool, args: Dict[str, Any]) -> bool:
    """Confirmation gate for write tools.

    Shows the user exactly what is about to happen to the outside world before
    anything fires.
    """
    console.print()
    console.print(
        Panel(
            Syntax(json.dumps(args, indent=2), "json", theme="ansi_dark", word_wrap=True),
            title="[bold yellow]Confirm: {0}[/]".format(tool.name),
            subtitle="[dim]{0}[/]".format(tool.description.split(".")[0]),
            border_style="yellow",
        )
    )
    return Confirm.ask("  Go ahead?", default=False)


def cmd_onboard(_: argparse.Namespace) -> int:
    apply_schema()
    onboarding.run_onboarding()
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    apply_schema()

    if onboarding.needs_onboarding():
        console.print("[yellow]No profile yet -- running onboarding first.[/]")
        onboarding.run_onboarding()

    agent = Agent(confirm=confirm_write)
    if not args.fresh:
        agent.load_history(limit=args.history)

    pending = store.pending_nudges()
    console.print()
    console.print(
        Panel(
            "Talking to your twin. [dim]Ctrl-C or 'exit' to leave. "
            "History {0}.[/]".format("off" if args.fresh else "on"),
            border_style="cyan",
        )
    )

    # Let the twin open the conversation when it has something queued -- being
    # proactive is the whole point of the persona.
    if pending:
        console.print()
        with console.status("[dim]thinking...[/]"):
            opening = agent.send(
                "(System: the user just opened a session. You have queued nudges. "
                "Open the conversation yourself with the most important one, in "
                "your own voice, citing the real data behind it.)"
            )
        console.print("[bold cyan]twin[/] " + opening)

    while True:
        console.print()
        try:
            message = console.input("[bold green]you[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/]")
            return 0

        if message.lower() in {"exit", "quit"}:
            console.print("[dim]bye.[/]")
            return 0
        if not message:
            continue

        console.print()
        with console.status("[dim]thinking...[/]"):
            reply = agent.send(message)
        console.print("[bold cyan]twin[/] " + reply)


def voice_confirm_write(tool: Tool, args: Dict[str, Any]) -> bool:
    """Confirmation gate for voice sessions.

    The prompt is announced aloud, but the answer stays **typed**. A
    mis-transcribed "yes" is exactly the failure mode that must not be able to
    send an email on the user's behalf, so speech never authorizes a write.
    """
    try:
        from twin.voice import tts

        tts.speak("I need you to confirm something on screen before I do it.")
    except Exception:
        # Never let a speech failure block the safety prompt.
        pass
    return confirm_write(tool, args)


def cmd_voice(args: argparse.Namespace) -> int:
    apply_schema()

    if onboarding.needs_onboarding():
        console.print("[yellow]No profile yet -- running onboarding first.[/]")
        onboarding.run_onboarding()

    from twin.voice import audio as voice_audio
    from twin.voice import session as voice_session

    if not SETTINGS.voice_enabled:
        console.print(
            "[red]Voice needs OPENAI_API_KEY in your .env[/] "
            "(it covers both Whisper and TTS)."
        )
        return 1

    if not voice_audio.has_input_device():
        console.print(
            "[red]No microphone available.[/] On macOS, grant microphone access "
            "to your terminal in System Settings > Privacy & Security."
        )
        return 1

    agent = Agent(confirm=voice_confirm_write)
    if not args.fresh:
        agent.load_history(limit=args.history)

    console.print()
    console.print(
        Panel(
            "Talking to your twin out loud.\n\n"
            "[dim]Press Enter to start recording, Enter again to stop. "
            "You can also just type. Say or type 'quit' to leave.\n"
            "Actions that send anything still need a typed confirmation.[/]",
            title="[bold]Voice[/]",
            border_style="cyan",
        )
    )
    return voice_session.run_voice_session(agent, console)


def cmd_slack_listen(_: argparse.Namespace) -> int:
    apply_schema()

    from twin.tools import slack_auth
    from twin.tools.slack_listener import SlackListener

    try:
        slack_auth.app_token()
    except slack_auth.NotConnected as exc:
        console.print("[red]{0}[/]".format(exc))
        return 1

    tokens = slack_auth.load_tokens() or {}
    listener = SlackListener()

    console.print()
    console.print(
        Panel(
            "Listening to [bold]{0}[/] over Socket Mode.\n\n"
            "[dim]Messages are indexed into recall as they arrive. Commitments "
            "you make{1} become tracked deadlines.\n"
            "No public endpoint is used -- this is an outbound connection.\n"
            "Ctrl-C to stop.[/]".format(
                tokens.get("team", "your workspace"),
                "" if tokens.get("human_user_id") else " (in DMs or mentioning the bot)",
            ),
            title="[bold]Slack listener[/]",
            border_style="cyan",
        )
    )

    try:
        listener.start()
    except KeyboardInterrupt:
        pass
    except slack_auth.NotConnected as exc:
        console.print("[red]{0}[/]".format(exc))
        return 1
    finally:
        listener.stop()

    console.print("\n[dim]listener stopped.[/]")
    return 0


def cmd_gmail_watch(args: argparse.Namespace) -> int:
    apply_schema()

    from twin.tools import gmail_watch
    from twin.tools.google_auth import NotConnected

    if not SETTINGS.gmail_push_enabled:
        console.print(
            "[red]Gmail push isn't configured.[/] Set GMAIL_PUBSUB_TOPIC and "
            "GMAIL_PUBSUB_SUBSCRIPTION in .env — see the README for the "
            "Google Cloud setup."
        )
        return 1

    try:
        if args.stop:
            gmail_watch.stop_watch()
            console.print("[green]Gmail watch stopped.[/]")
            return 0

        if args.start:
            result = gmail_watch.start_watch()
            console.print(
                "[green]Watch registered.[/] Expires {0}.".format(
                    result["expires_at"].strftime("%a %d %b %H:%M") if result["expires_at"] else "?"
                )
            )

        state = gmail_watch.status()
    except NotConnected as exc:
        console.print("[red]{0}[/]".format(exc))
        return 1
    except Exception as exc:
        console.print("[red]Gmail watch failed: {0}[/]".format(exc))
        return 1

    console.print()
    console.print(
        Panel(
            "Topic: {0}\nSubscription: {1}\nHistory cursor: {2}\n"
            "Watch expires: {3}\nNeeds renewal: {4}".format(
                state["topic"],
                state["subscription"],
                state["history_id"] or "(not set — run with --start)",
                state["expires_at"].strftime("%a %d %b %H:%M") if state["expires_at"] else "not registered",
                "yes" if state["needs_renewal"] else "no",
            ),
            title="[bold]Gmail push[/]",
            border_style="cyan",
        )
    )

    if args.pull:
        result = gmail_watch.process_notifications()
        console.print(
            "Pulled {0} notification(s) → {1} message(s) → {2} deadline(s).{3}".format(
                result["pulled"],
                result["messages"],
                result["deadlines"],
                " [yellow]History cursor had expired; re-swept.[/]" if result["recovered"] else "",
            )
        )
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from twin import service

    try:
        if args.uninstall:
            removed = service.uninstall()
            console.print(
                "[green]Background service removed.[/]" if removed
                else "[yellow]Nothing was installed.[/]"
            )
            return 0

        if args.install:
            result = service.install(args.interval)
            console.print()
            console.print(
                Panel(
                    "Proactive checks now run every {0} minutes, whether or not "
                    "a terminal is open.\n\n"
                    "Agent: [dim]{1}[/]\n"
                    "Logs:  [dim]{2}[/]\n\n"
                    "[yellow]The first background run may prompt for keychain "
                    "access[/] — the checks read your stored integration "
                    "tokens. Choose [bold]Always Allow[/], otherwise background "
                    "runs will silently fail to reach your accounts.".format(
                        result["interval"] // 60, result["plist"], result["logs"][0]
                    ),
                    title="[bold green]Service installed[/]",
                    border_style="green",
                )
            )

        if args.run_now:
            service.run_now()
            console.print("[green]Triggered a run.[/]")

        state = service.status()
        console.print()
        console.print(
            Panel(
                "Installed: {0}\nRunning:   {1}\nAgent:     {2}\nLogs:      {3}".format(
                    "yes" if state["installed"] else "no",
                    "yes" if state["loaded"] else "no",
                    state["plist"],
                    state["stdout_log"],
                ),
                title="[bold]Background service[/]",
                border_style="cyan",
            )
        )

        if args.logs:
            console.print("\n[bold]Recent output[/]")
            console.print(service.tail_log(args.logs))
            errors = service.tail_log(args.logs, error_log=True)
            if errors and not errors.startswith("(no log"):
                console.print("\n[bold]Errors[/]")
                console.print("[red]{0}[/]".format(errors))
        return 0

    except service.ServiceError as exc:
        console.print("[red]{0}[/]".format(exc))
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    apply_schema()

    from twin.web import api as web_api

    console.print()
    console.print(
        Panel(
            "Open [bold]http://{0}:{1}[/]\n\n"
            "[dim]Bound to localhost — v1 is not a networked app, and there's no "
            "auth because there's nothing to authenticate against.\n"
            "Ctrl-C to stop.[/]".format(args.host, args.port),
            title="[bold]Michi[/]",
            border_style="cyan",
        )
    )
    try:
        web_api.serve(host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]stopped.[/]")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    apply_schema()
    profile = store.get_profile()
    now = dt.datetime.now(dt.timezone.utc)

    console.print()
    console.print(
        Panel(
            "Onboarded: {0}\nConnected: {1}\nInterests: {2}\n"
            "Available: {3:02d}:00-{4:02d}:00 ({5})\nBased in: {7}\n"
            "Staleness threshold: {6} days".format(
                profile["onboarded_at"].strftime("%Y-%m-%d") if profile["onboarded_at"] else "no",
                ", ".join(profile["connected_apps"]) or "nothing",
                ", ".join(profile["interests"]) or "none recorded",
                int((profile.get("active_hours") or {}).get("start", 9)),
                int((profile.get("active_hours") or {}).get("end", 22)),
                profile.get("timezone") or "UTC",
                profile["staleness_threshold_days"],
                profile.get("location") or "not set",
            ),
            title="[bold]Twin profile[/]",
            border_style="cyan",
        )
    )

    projects = store.list_projects()
    if projects:
        table = Table(title="Projects", header_style="bold", box=None)
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("Last activity")
        for project in projects:
            days = (now - project["last_activity_at"]).days
            stale = days >= profile["staleness_threshold_days"]
            table.add_row(
                project["name"],
                project["status"],
                "[red]{0}d ago[/]".format(days) if stale else "{0}d ago".format(days),
            )
        console.print(table)

    deadlines = store.list_open_deadlines()
    if deadlines:
        table = Table(title="Open deadlines", header_style="bold", box=None)
        table.add_column("#")
        table.add_column("Description")
        table.add_column("Due")
        for deadline in deadlines:
            table.add_row(
                str(deadline["id"]),
                deadline["description"],
                deadline["due_at"].strftime("%Y-%m-%d %H:%M"),
            )
        console.print(table)

    facts = profile.get("facts") or {}
    if facts:
        table = Table(title="Learned facts", header_style="bold", box=None)
        table.add_column("Key")
        table.add_column("Value")
        for key, entry in facts.items():
            table.add_row(key, str(entry.get("value")))
        console.print(table)

    pending = store.pending_nudges()
    console.print(
        "\n[dim]{0} nudge(s) queued and waiting for your next session.[/]".format(len(pending))
    )
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    apply_schema()
    for line in scheduler.run_once():
        console.print("  " + line)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    apply_schema()
    console.print(
        "[dim]Running proactive checks every {0} minutes. Ctrl-C to stop.[/]".format(
            args.interval
        )
    )
    try:
        scheduler.run_forever(interval_minutes=args.interval)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[dim]stopped.[/]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="twin", description="Personal digital-twin assistant")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("onboard", help="first-run setup / change settings").set_defaults(
        func=cmd_onboard
    )

    chat = subparsers.add_parser("chat", help="talk to your twin")
    chat.add_argument("--fresh", action="store_true", help="ignore prior conversation history")
    chat.add_argument("--history", type=int, default=20, help="turns of history to replay")
    chat.set_defaults(func=cmd_chat)

    voice = subparsers.add_parser("voice", help="talk to your twin out loud")
    voice.add_argument("--fresh", action="store_true", help="ignore prior conversation history")
    voice.add_argument("--history", type=int, default=20, help="turns of history to replay")
    voice.set_defaults(func=cmd_voice)

    subparsers.add_parser(
        "slack-listen", help="stream Slack events over Socket Mode"
    ).set_defaults(func=cmd_slack_listen)

    gmail_watch_cmd = subparsers.add_parser(
        "gmail-watch", help="register/inspect Gmail push notifications"
    )
    gmail_watch_cmd.add_argument("--start", action="store_true", help="register the watch")
    gmail_watch_cmd.add_argument("--stop", action="store_true", help="cancel the watch")
    gmail_watch_cmd.add_argument("--pull", action="store_true", help="drain pending notifications now")
    gmail_watch_cmd.set_defaults(func=cmd_gmail_watch)

    serve = subparsers.add_parser("serve", help="open the web UI")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1", help="localhost only by default")
    serve.set_defaults(func=cmd_serve)

    subparsers.add_parser("status", help="what the twin currently knows").set_defaults(
        func=cmd_status
    )
    subparsers.add_parser("check", help="run proactive checks once").set_defaults(func=cmd_check)

    watch = subparsers.add_parser("watch", help="run proactive checks on an interval")
    watch.add_argument("--interval", type=int, default=30, help="minutes between checks")
    watch.set_defaults(func=cmd_watch)

    svc = subparsers.add_parser(
        "service", help="run proactive checks in the background via launchd"
    )
    svc.add_argument("--install", action="store_true", help="install and start the agent")
    svc.add_argument("--uninstall", action="store_true", help="stop and remove the agent")
    svc.add_argument(
        "--interval", type=int, default=1800, help="seconds between checks (default 1800)"
    )
    svc.add_argument("--run-now", action="store_true", help="trigger a run immediately")
    svc.add_argument(
        "--logs", type=int, nargs="?", const=20, default=0, help="show the last N log lines"
    )
    svc.set_defaults(func=cmd_service)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
