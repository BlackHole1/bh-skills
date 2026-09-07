"""Tests for ship-pr/scripts/pr_worktree.py.

Mode selection (worktree-in-repo vs clone) and the exact git call sequences are
exercised through the module's _gh/_git seams with a recording fake runner, so
no gh, network, or auth is involved. Path computation and the destructive-free
subcommands run as real subprocesses with TMPDIR pinned inside the test tmp
dir (the shell used ${TMPDIR:-/tmp}; the port uses tempfile.gettempdir(),
which honors TMPDIR on every platform).
"""

from __future__ import annotations

import os
import stat

import pytest

import pr_worktree
from gh_retry import RunResult


class FakeRunner:
    """Returns the first rule whose argv prefix matches; records every call."""

    def __init__(self, rules=()):
        self.rules = [(list(prefix), result) for prefix, result in rules]
        self.calls = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        for prefix, result in self.rules:
            if cmd[: len(prefix)] == prefix:
                return result
        return RunResult("", "", 0)


class ExistenceProbe:
    """Wraps a seam runner and records whether `path` still existed at the
    moment a call matching `prefix` was made. The stale-entry cleanup has to
    happen BEFORE the create call, or the real git/gh would fail on an occupied
    path — asserting only on the end state would not catch that."""

    def __init__(self, inner, prefix, path):
        self.inner = inner
        self.prefix = list(prefix)
        self.path = path
        self.existed = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if cmd[: len(self.prefix)] == self.prefix:
            self.existed.append(os.path.lexists(self.path))
        return self.inner(cmd, **kwargs)


def make_symlink_or_skip(link, target):
    """Create link -> target, skipping the test where the platform forbids it:
    on Windows, creating a symlink needs privilege or developer mode."""
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this platform")


VIEW = ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]


@pytest.fixture
def pin_tmp(monkeypatch, tmp_path):
    """Pin tempfile.gettempdir() for in-process calls (it caches TMPDIR, so
    setenv alone would not work) and return the pinned base."""
    monkeypatch.setattr(pr_worktree.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def wt_for(tmp_path, slug_dash, pr):
    return os.path.join(str(tmp_path), "ship-pr-worktrees", "{}-pr-{}".format(slug_dash, pr))


# ---- path computation --------------------------------------------------------


def test_worktree_path_slug_and_base(pin_tmp):
    assert pr_worktree.worktree_path("o/r", "5") == wt_for(pin_tmp, "o-r", "5")
    # Every slash in the slug becomes a dash.
    assert pr_worktree.worktree_path("a/b/c", "9") == wt_for(pin_tmp, "a-b-c", "9")


def test_cli_path_prints_location_without_changes(run_script, tmp_path):
    r = run_script(
        "ship-pr",
        "pr_worktree.py",
        "path",
        "12",
        "octo/hello-world",
        env={"TMPDIR": str(tmp_path)},
        cwd=tmp_path,
    )
    assert r.returncode == 0
    assert r.stdout == wt_for(tmp_path, "octo-hello-world", "12") + "\n"
    # 'path' must not create anything.
    assert not (tmp_path / "ship-pr-worktrees").exists()


# ---- usage / dispatch --------------------------------------------------------


def test_cli_usage_exits_2(run_script, tmp_path):
    for args in ([], ["ensure"]):
        r = run_script("ship-pr", "pr_worktree.py", *args, cwd=tmp_path)
        assert r.returncode == 2
        assert "usage: pr_worktree.py ensure|remove|path <PR> [OWNER/REPO]" in r.stderr


def test_cli_unknown_command_exits_2(run_script, tmp_path):
    r = run_script(
        "ship-pr", "pr_worktree.py", "frob", "5", "o/r",
        env={"TMPDIR": str(tmp_path)}, cwd=tmp_path,
    )
    assert r.returncode == 2
    assert r.stderr == "unknown command: frob\n"


def test_cannot_resolve_repo_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(pr_worktree, "_gh", FakeRunner([(VIEW, RunResult("", "no repo", 1))]))
    assert pr_worktree.main(["path", "5"]) == 1
    assert capsys.readouterr().err == (
        "cannot resolve repo (pass OWNER/REPO, or run inside the target repo)\n"
    )


def test_gh_repo_env_fallback(monkeypatch, pin_tmp, capsys):
    monkeypatch.setenv("GH_REPO", "env/repo")
    assert pr_worktree.main(["path", "3"]) == 0
    assert capsys.readouterr().out == wt_for(pin_tmp, "env-repo", "3") + "\n"


# ---- remove ------------------------------------------------------------------


def test_cli_remove_deletes_hand_built_directory(run_script, tmp_path):
    wt = tmp_path / "ship-pr-worktrees" / "o-r-pr-5"
    wt.mkdir(parents=True)
    (wt / "file.txt").write_text("x", encoding="utf-8")
    # cwd is not a git repo: the worktree remove/prune calls fail harmlessly
    # (the shell silenced them with 2>/dev/null) and rmtree does the cleanup.
    r = run_script(
        "ship-pr", "pr_worktree.py", "remove", "5", "o/r",
        env={"TMPDIR": str(tmp_path)}, cwd=tmp_path,
    )
    assert r.returncode == 0
    assert r.stdout == "removed {}\n".format(wt)
    assert not wt.exists()


def test_remove_handles_readonly_files(monkeypatch, pin_tmp, capsys):
    # Windows clone-mode .git objects are read-only, which makes plain rmtree
    # fail there; the chmod-and-retry handler must still delete them. (POSIX
    # unlinks read-only files from a writable dir anyway — same as rm -rf.)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    wt = pr_worktree.worktree_path("o/r", "8")
    locked = os.path.join(wt, "objects")
    os.makedirs(locked)
    with open(os.path.join(locked, "pack"), "w", encoding="utf-8") as fh:
        fh.write("x")
    os.chmod(os.path.join(locked, "pack"), 0o400)
    assert pr_worktree.main(["remove", "8", "o/r"]) == 0
    assert capsys.readouterr().out == "removed {}\n".format(wt)
    assert not os.path.exists(wt)


def test_remove_deletes_a_plain_file_before_printing_success(monkeypatch, pin_tmp):
    # rm -rf deletes whatever sits at the path, but shutil.rmtree refuses a
    # non-directory: a stale FILE used to survive `remove`, which still printed
    # its "removed <path>" line — a false success a consumer parses.
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    wt = pr_worktree.worktree_path("o/r", "7")
    os.makedirs(os.path.dirname(wt))
    with open(wt, "w", encoding="utf-8") as fh:
        fh.write("stale\n")
    printed = []
    monkeypatch.setattr("builtins.print", lambda line: printed.append((line, os.path.lexists(wt))))
    assert pr_worktree.main(["remove", "7", "o/r"]) == 0
    assert printed == [("removed {}".format(wt), False)]


def test_remove_unlinks_a_symlink_without_following_it(monkeypatch, pin_tmp, capsys):
    # rm -rf unlinks a symlink-to-a-directory; it never descends through one.
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    wt = pr_worktree.worktree_path("o/r", "9")
    os.makedirs(os.path.dirname(wt))
    victim = pin_tmp / "not-a-worktree"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    make_symlink_or_skip(wt, str(victim))
    assert pr_worktree.main(["remove", "9", "o/r"]) == 0
    assert capsys.readouterr().out == "removed {}\n".format(wt)
    assert not os.path.lexists(wt)
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_undeletable_symlink_is_never_chmodded_through(tmp_path):
    # os.chmod FOLLOWS symlinks, so the read-only-bit retry used to change the
    # mode of the link's TARGET — a write OUTSIDE the worktree that rm -rf never
    # performs, on the very path the docstring promises is unlinked and never
    # followed. Setup: a link whose parent is mode 0o500, so os.remove fails
    # with EACCES and the retry branch runs.
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(str(victim), 0o755)
    holder = tmp_path / "holder"
    holder.mkdir()
    link = holder / "worktree"
    make_symlink_or_skip(str(link), str(victim))
    os.chmod(str(holder), 0o500)
    try:
        if os.access(str(holder), os.W_OK):
            # Windows, root, or a filesystem that ignores the mode: nothing here
            # would make os.remove fail, so the scenario cannot be staged.
            pytest.skip("directory permissions are not enforced for this user")
        pr_worktree._rmtree_force(str(link))
        assert os.path.lexists(str(link))  # left alone, silently
        assert stat.S_IMODE(os.stat(str(victim)).st_mode) == 0o755
    finally:
        os.chmod(str(holder), 0o700)


def test_remove_is_silent_when_nothing_is_there(monkeypatch, pin_tmp, capsys):
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    wt = pr_worktree.worktree_path("o/r", "11")
    assert pr_worktree.main(["remove", "11", "o/r"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "removed {}\n".format(wt)
    assert captured.err == ""


# ---- ensure: mode selection and git-op sequencing ----------------------------


def test_ensure_worktree_mode_fresh_add(monkeypatch, pin_tmp, capsys):
    # cwd IS the target repo -> detached worktree sharing the object store.
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner([(["git", "rev-parse", "FETCH_HEAD"], RunResult("abc123\n", "", 0))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    wt = pr_worktree.worktree_path("o/r", "5")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert gh.calls == [VIEW]
    assert git.calls == [
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "fetch", "-q", "origin", "pull/5/head"],
        ["git", "rev-parse", "FETCH_HEAD"],
        ["git", "worktree", "prune"],
        ["git", "worktree", "add", "-q", "--detach", wt, "abc123"],
    ]


def test_ensure_worktree_mode_reuses_when_dotgit_is_a_file(monkeypatch, pin_tmp, capsys):
    # In a linked worktree .git is a FILE (gitdir pointer) — reuse hard-resets.
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner([(["git", "rev-parse", "FETCH_HEAD"], RunResult("abc123\n", "", 0))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    wt = pr_worktree.worktree_path("o/r", "5")
    os.makedirs(wt)
    with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as fh:
        fh.write("gitdir: elsewhere\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert git.calls == [
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "fetch", "-q", "origin", "pull/5/head"],
        ["git", "rev-parse", "FETCH_HEAD"],
        ["git", "-C", wt, "reset", "-q", "--hard", "abc123"],
        ["git", "-C", wt, "clean", "-qfd"],
    ]


def test_ensure_worktree_mode_self_heals_a_stale_file(monkeypatch, pin_tmp, capsys):
    # A FILE at the worktree path is not a worktree: the shell rm -rf'd it and
    # re-added the worktree. The port must self-heal the same way instead of
    # leaving it there and dying with "worktree add failed".
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner([(["git", "rev-parse", "FETCH_HEAD"], RunResult("abc123\n", "", 0))])
    wt = pr_worktree.worktree_path("o/r", "5")
    probe = ExistenceProbe(git, ["git", "worktree", "add"], wt)
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", probe)
    os.makedirs(os.path.dirname(wt))
    with open(wt, "w", encoding="utf-8") as fh:
        fh.write("stale\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert probe.existed == [False]  # cleared before the add, not after
    assert git.calls[-1] == ["git", "worktree", "add", "-q", "--detach", wt, "abc123"]


def test_ensure_worktree_mode_self_heals_a_stale_symlink(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner([(["git", "rev-parse", "FETCH_HEAD"], RunResult("abc123\n", "", 0))])
    wt = pr_worktree.worktree_path("o/r", "5")
    probe = ExistenceProbe(git, ["git", "worktree", "add"], wt)
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", probe)
    os.makedirs(os.path.dirname(wt))
    victim = pin_tmp / "not-a-worktree"
    victim.mkdir()
    make_symlink_or_skip(wt, str(victim))
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert probe.existed == [False]
    assert victim.is_dir()  # unlinked, not walked


def test_ensure_clone_mode_when_cwd_is_another_repo(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    git = FakeRunner()
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    wt = pr_worktree.worktree_path("o/r", "5")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert gh.calls == [VIEW, ["gh", "repo", "clone", "o/r", wt, "--", "-q"]]
    # Short-circuit: slug mismatch means rev-parse --is-inside-work-tree never ran.
    assert git.calls == [
        ["git", "-C", wt, "fetch", "-q", "origin", "pull/5/head"],
        ["git", "-C", wt, "checkout", "-q", "--detach", "FETCH_HEAD"],
        ["git", "-C", wt, "reset", "-q", "--hard", "FETCH_HEAD"],
        ["git", "-C", wt, "clean", "-qfd"],
    ]


def test_ensure_clone_mode_outside_any_work_tree(monkeypatch, pin_tmp):
    # Same slug but NOT inside a work tree -> clone mode, not worktree mode.
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner(
        [(["git", "rev-parse", "--is-inside-work-tree"], RunResult("", "fatal: not a git repository", 128))]
    )
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    wt = pr_worktree.worktree_path("o/r", "5")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert gh.calls == [VIEW, ["gh", "repo", "clone", "o/r", wt, "--", "-q"]]
    assert git.calls[0] == ["git", "rev-parse", "--is-inside-work-tree"]
    assert git.calls[1] == ["git", "-C", wt, "fetch", "-q", "origin", "pull/5/head"]


def test_ensure_clone_mode_reuses_existing_clone(monkeypatch, pin_tmp, capsys):
    # In a full clone .git is a DIRECTORY — no re-clone, just re-sync.
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    git = FakeRunner()
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    wt = pr_worktree.worktree_path("o/r", "5")
    os.makedirs(os.path.join(wt, ".git"))
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert gh.calls == [VIEW]  # no clone
    assert os.path.isdir(os.path.join(wt, ".git"))  # and no rmtree
    assert git.calls[0] == ["git", "-C", wt, "fetch", "-q", "origin", "pull/5/head"]


def test_ensure_clone_mode_self_heals_a_stale_file(monkeypatch, pin_tmp, capsys):
    # Same self-heal on the clone side: the stale entry has to be gone before
    # `gh repo clone` runs, or the clone fails on an occupied path.
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    wt = pr_worktree.worktree_path("o/r", "5")
    probe = ExistenceProbe(gh, ["gh", "repo", "clone"], wt)
    monkeypatch.setattr(pr_worktree, "_gh", probe)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    os.makedirs(os.path.dirname(wt))
    with open(wt, "w", encoding="utf-8") as fh:
        fh.write("stale\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert probe.existed == [False]
    assert gh.calls[-1] == ["gh", "repo", "clone", "o/r", wt, "--", "-q"]


def test_ensure_clone_mode_self_heals_a_stale_symlink(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    wt = pr_worktree.worktree_path("o/r", "5")
    probe = ExistenceProbe(gh, ["gh", "repo", "clone"], wt)
    monkeypatch.setattr(pr_worktree, "_gh", probe)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    os.makedirs(os.path.dirname(wt))
    victim = pin_tmp / "not-a-worktree"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    make_symlink_or_skip(wt, str(victim))
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    assert capsys.readouterr().out == wt + "\n"
    assert probe.existed == [False]
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_clone_mode_reports_a_cleanup_that_did_not_clear_the_path(
    monkeypatch, pin_tmp, capsys
):
    # The shell silenced rm -rf everywhere EXCEPT clone mode, where a failing
    # cleanup printed rm's diagnostic before the clone died on the occupied
    # path. _rmtree_force is unconditionally silent, so that breadcrumb is
    # restored explicitly — without it "clone failed" states no cause.
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    monkeypatch.setattr(pr_worktree, "_rmtree_force", lambda path: None)  # cleanup fails
    wt = pr_worktree.worktree_path("o/r", "5")
    os.makedirs(os.path.dirname(wt))
    with open(wt, "w", encoding="utf-8") as fh:
        fh.write("stale\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "rm -rf {} failed (path still exists)\n".format(wt)
    # Exit contract unchanged: the clone still runs and still decides the rc.
    assert gh.calls[-1] == ["gh", "repo", "clone", "o/r", wt, "--", "-q"]
    assert captured.out == wt + "\n"


def test_worktree_mode_stays_silent_when_cleanup_does_not_clear_the_path(
    monkeypatch, pin_tmp, capsys
):
    # The mirror of the test above: the shell DID silence rm -rf on this path,
    # so no diagnostic belongs here.
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner([(["git", "rev-parse", "FETCH_HEAD"], RunResult("abc123\n", "", 0))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    monkeypatch.setattr(pr_worktree, "_rmtree_force", lambda path: None)
    wt = pr_worktree.worktree_path("o/r", "5")
    os.makedirs(os.path.dirname(wt))
    with open(wt, "w", encoding="utf-8") as fh:
        fh.write("stale\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == wt + "\n"


def test_ensure_degrades_when_the_base_path_is_occupied(monkeypatch, pin_tmp, capsys):
    # The shell's `mkdir -p` printed one diagnostic and carried on (no set -e);
    # an occupied base must not become an unhandled OSError traceback.
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    wt = pr_worktree.worktree_path("o/r", "5")
    base = os.path.dirname(wt)
    with open(base, "w", encoding="utf-8") as fh:  # a FILE where the dir belongs
        fh.write("occupied\n")
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 0
    captured = capsys.readouterr()
    assert captured.out == wt + "\n"
    assert captured.err.startswith("mkdir -p {} failed: ".format(base))
    assert captured.err.count("\n") == 1  # one diagnostic line, then carry on
    assert gh.calls[-1] == ["gh", "repo", "clone", "o/r", wt, "--", "-q"]


def test_ensure_fetch_failure_exits_1_worktree_mode(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner(
        [(["git", "fetch"], RunResult("", "fatal: couldn't find remote ref\n", 128))]
    )
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # git's own stderr is forwarded (the shell did not silence this call).
    assert captured.err == "fatal: couldn't find remote ref\nfetch pull/5/head failed\n"


def test_ensure_fetch_failure_exits_1_clone_mode(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("someone/else\n", "", 0))])
    git = FakeRunner([(["git", "-C"], RunResult("", "", 1))])
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 1
    assert capsys.readouterr().err == "fetch pull/5/head failed\n"


def test_ensure_clone_failure_exits_1(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner(
        [
            (VIEW, RunResult("someone/else\n", "", 0)),
            (["gh", "repo", "clone"], RunResult("", "gh: repository not found\n", 1)),
        ]
    )
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", FakeRunner())
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 1
    assert capsys.readouterr().err == "gh: repository not found\nclone failed\n"


def test_ensure_worktree_add_failure_exits_1(monkeypatch, pin_tmp, capsys):
    gh = FakeRunner([(VIEW, RunResult("o/r\n", "", 0))])
    git = FakeRunner(
        [(["git", "worktree", "add"], RunResult("", "fatal: already exists\n", 128))]
    )
    monkeypatch.setattr(pr_worktree, "_gh", gh)
    monkeypatch.setattr(pr_worktree, "_git", git)
    assert pr_worktree.main(["ensure", "5", "o/r"]) == 1
    assert capsys.readouterr().err == "fatal: already exists\nworktree add failed\n"
