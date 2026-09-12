"""Background scheduling via launchd.

The persona only works if the proactive checks actually run. Left in the
foreground, `watch` stops the moment a terminal closes, which quietly undoes
the premise of the whole thing.

This registers a **LaunchAgent** that runs `twin.cli check` on an interval,
rather than keeping `watch` alive under `KeepAlive`. launchd already does
scheduling, supervision, restart-on-crash, and catch-up after sleep; a
long-lived APScheduler process would duplicate all of that and add a thing
that can die without anyone noticing.

A user *agent* (not a daemon) is required: the credential store is the login
keychain, which only exists inside a GUI login session.
"""

import getpass
import os
import plistlib
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

LABEL = "com.digitaltwin.scheduler"

DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes
MIN_INTERVAL_SECONDS = 300

LOG_DIR = os.path.expanduser("~/Library/Logs/digital-twin")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")


class ServiceError(RuntimeError):
    """Installing or querying the service failed."""


def plist_path() -> str:
    return os.path.join(AGENTS_DIR, "{0}.plist".format(LABEL))


def log_paths() -> Tuple[str, str]:
    return (
        os.path.join(LOG_DIR, "scheduler.log"),
        os.path.join(LOG_DIR, "scheduler.err.log"),
    )


def project_root() -> str:
    """The directory holding the `twin` package -- i.e. where .env lives."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_plist(
    python_executable: str,
    project_dir: str,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """Construct the LaunchAgent definition.

    Pure, so the generated structure can be asserted on without touching the
    user's LaunchAgents directory.
    """
    interval = max(int(interval_seconds), MIN_INTERVAL_SECONDS)
    stdout_log, stderr_log = log_paths()

    return {
        "Label": LABEL,
        "ProgramArguments": [python_executable, "-m", "twin.cli", "check"],
        # Both matter: python-dotenv resolves .env relative to the working
        # directory, and the package is imported by path rather than installed.
        "WorkingDirectory": project_dir,
        "EnvironmentVariables": {
            "PYTHONPATH": project_dir,
            # launchd gives a process a near-empty PATH; Postgres client
            # tooling and Homebrew binaries live in /usr/local/bin.
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": os.path.expanduser("~"),
        },
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": stdout_log,
        "StandardErrorPath": stderr_log,
        # Deprioritises the job so a check never competes with foreground work.
        "ProcessType": "Background",
        # The checks are idempotent, but overlapping runs would waste API
        # calls, so launchd is told not to start one while another is running.
        "AbandonProcessGroup": False,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def _domain_target() -> str:
    return "gui/{0}".format(os.getuid())


def _service_target() -> str:
    return "{0}/{1}".format(_domain_target(), LABEL)


def is_installed() -> bool:
    return os.path.exists(plist_path())


def is_loaded() -> bool:
    return _launchctl("print", _service_target()).returncode == 0


def install(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> Dict[str, Any]:
    """Write the plist and load it. Safe to re-run; replaces any existing job."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    definition = build_plist(sys.executable, project_root(), interval_seconds)
    path = plist_path()

    with open(path, "wb") as handle:
        plistlib.dump(definition, handle)

    # Replace rather than stack: bootout first, ignoring "not loaded".
    if is_loaded():
        _launchctl("bootout", _service_target())

    result = _launchctl("bootstrap", _domain_target(), path)
    if result.returncode != 0:
        # Older macOS only understands load/unload.
        fallback = _launchctl("load", "-w", path)
        if fallback.returncode != 0:
            raise ServiceError(
                "launchctl refused to load the agent:\n{0}\n{1}".format(
                    result.stderr.strip(), fallback.stderr.strip()
                )
            )

    return {
        "plist": path,
        "interval": definition["StartInterval"],
        "logs": log_paths(),
    }


def uninstall() -> bool:
    """Unload and remove the agent. Returns False if it wasn't installed."""
    path = plist_path()
    existed = os.path.exists(path)

    if is_loaded():
        result = _launchctl("bootout", _service_target())
        if result.returncode != 0:
            _launchctl("unload", "-w", path)

    if existed:
        os.unlink(path)
    return existed


def run_now() -> None:
    """Force an immediate run, rather than waiting for the next interval."""
    result = _launchctl("kickstart", "-k", _service_target())
    if result.returncode != 0:
        raise ServiceError(
            "Could not trigger a run: {0}".format(result.stderr.strip() or "unknown error")
        )


def tail_log(lines: int = 20, error_log: bool = False) -> str:
    stdout_log, stderr_log = log_paths()
    path = stderr_log if error_log else stdout_log
    if not os.path.exists(path):
        return "(no log yet at {0})".format(path)
    with open(path, "r", errors="replace") as handle:
        return "".join(handle.readlines()[-lines:]).rstrip() or "(empty)"


def status() -> Dict[str, Any]:
    stdout_log, stderr_log = log_paths()
    return {
        "installed": is_installed(),
        "loaded": is_loaded(),
        "plist": plist_path(),
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
        "user": getpass.getuser(),
    }
