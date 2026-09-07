"""Shared fixtures for the skill-script tests.

The scripts under */scripts are importable as plain modules (pyproject.toml puts
each scripts/ dir on pythonpath), so unit tests import functions directly and
monkeypatch the gh seams. Integration tests run the scripts as subprocesses
against throwaway git repos built here.

Everything that touches git or gh runs with an isolated environment: a private
HOME / git config (pinned identity, gpgsign off, autocrlf off, no hooksPath) and
no gh auth or tokens. That keeps results deterministic on any developer machine
and on the Windows/macOS/Linux CI runners, and guarantees a test can never read
or mutate the real user's GitHub state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "commit": REPO_ROOT / "commit" / "scripts",
    "create-pr": REPO_ROOT / "create-pr" / "scripts",
    "ship-pr": REPO_ROOT / "ship-pr" / "scripts",
}

# Env vars the scripts read as seams or knobs — always start tests without them.
_SCRIPT_SEAMS = [
    "GH_REPO",
    "GH_RETRY_TRIES",
    "GH_RETRY_DELAY",
    "PRC_THREADS_FILE",
    "PRC_SUMMARY_FILE",
    "PR_WATCH_ONCE",
    "PR_WATCH_INTERVAL",
    "PR_WATCH_SEED_FILE",
    "PR_DIFF_MAX_LINES",
    "COMMIT_DIFF_MAX_LINES",
    "COMMIT_PROTECTED_BRANCHES",
    "SKILLS_LANG_KEY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GH_TOKEN",
    "GITHUB_TOKEN",
]


@pytest.fixture(scope="session")
def isolated_env(tmp_path_factory):
    """A process environment cut off from the user's git/gh configuration."""
    home = tmp_path_factory.mktemp("home")
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        "[user]\n"
        "\temail = skills-test@example.com\n"
        "\tname = Skills Test\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
        "[commit]\n"
        "\tgpgsign = false\n"
        "[core]\n"
        "\tautocrlf = false\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    for var in _SCRIPT_SEAMS:
        env.pop(var, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GH_CONFIG_DIR": str(home / "gh"),
            # gh must never prompt or open a browser from a test.
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
        }
    )
    return env


@pytest.fixture
def sh(isolated_env):
    """Run a command list under the isolated env; returns CompletedProcess."""

    def run(cmd, cwd=None, stdin=None, env=None, timeout=60):
        full_env = dict(isolated_env)
        if env:
            full_env.update(env)
        return subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
            timeout=timeout,
        )

    return run


@pytest.fixture
def run_script(sh):
    """Run one of the skill scripts as a subprocess with this interpreter."""

    def run(skill, module, *args, cwd=None, stdin=None, env=None, timeout=60):
        return sh(
            [sys.executable, SCRIPTS[skill] / module, *args],
            cwd=cwd,
            stdin=stdin,
            env=env,
            timeout=timeout,
        )

    return run


@pytest.fixture
def git_repo(tmp_path, sh):
    """Factory for throwaway git repos (branch 'main', pinned identity)."""

    def make(name="repo"):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        r = sh(["git", "init", "-q", "-b", "main"], cwd=path)
        assert r.returncode == 0, r.stderr
        return path

    return make


@pytest.fixture
def git_commit(sh):
    """Stage everything and commit; returns the short SHA."""

    def commit(repo, message="chore: test commit"):
        sh(["git", "add", "-A"], cwd=repo)
        r = sh(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=repo)
        assert r.returncode == 0, r.stderr
        return sh(["git", "rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()

    return commit
