"""LaunchAgent definition.

Only the pure plist construction is tested -- installing genuinely mutates
~/Library/LaunchAgents, which a test suite has no business doing. The fields
covered here are the ones whose absence causes a silent failure: a missing
WorkingDirectory means .env is never found, and a missing PYTHONPATH means the
package can't be imported.
"""

import plistlib

from twin import service


def plist(**overrides):
    args = {
        "python_executable": "/proj/.venv/bin/python",
        "project_dir": "/proj",
        "interval_seconds": 1800,
    }
    args.update(overrides)
    return service.build_plist(**args)


def test_runs_check_not_watch():
    """launchd does the scheduling; a long-lived watch would duplicate it."""
    assert plist()["ProgramArguments"] == [
        "/proj/.venv/bin/python", "-m", "twin.cli", "check"
    ]


def test_working_directory_is_set():
    """python-dotenv resolves .env relative to the working directory."""
    assert plist()["WorkingDirectory"] == "/proj"


def test_pythonpath_is_set():
    """The package is imported by path, not installed into the venv."""
    assert plist()["EnvironmentVariables"]["PYTHONPATH"] == "/proj"


def test_path_includes_homebrew_and_usr_local():
    """launchd hands a process a near-empty PATH."""
    path = plist()["EnvironmentVariables"]["PATH"]
    assert "/usr/local/bin" in path and "/opt/homebrew/bin" in path


def test_home_is_set():
    """The keychain backend needs HOME to locate the login keychain."""
    assert plist()["EnvironmentVariables"]["HOME"]


def test_interval_is_carried_through():
    assert plist(interval_seconds=3600)["StartInterval"] == 3600


def test_interval_is_clamped_to_a_floor():
    """A tight interval would burn API credits on every tick."""
    assert plist(interval_seconds=5)["StartInterval"] == service.MIN_INTERVAL_SECONDS


def test_runs_at_load():
    assert plist()["RunAtLoad"] is True


def test_marked_as_background_process():
    assert plist()["ProcessType"] == "Background"


def test_logs_are_redirected_to_files():
    definition = plist()
    assert definition["StandardOutPath"].endswith("scheduler.log")
    assert definition["StandardErrorPath"].endswith("scheduler.err.log")


def test_label_is_stable():
    """The label identifies the job to launchctl; changing it orphans the old one."""
    assert plist()["Label"] == service.LABEL


def test_definition_serializes_to_valid_plist():
    """plistlib rejects unsupported types, so this catches a bad field early."""
    encoded = plistlib.dumps(plist())
    assert plistlib.loads(encoded)["Label"] == service.LABEL


def test_paths_are_absolute():
    definition = plist()
    assert definition["StandardOutPath"].startswith("/")
    assert definition["WorkingDirectory"].startswith("/")
