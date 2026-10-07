"""github.py against the fake GitHub: real git on bare repos, `gh` answered in-process."""

from __future__ import annotations

import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fake_gh import git

from crab_student import core, github


def error(fn, *args) -> core.CrabError:
    with pytest.raises(core.CrabError) as e:
        fn(*args)
    return e.value


def ref(repo: Path, name: str) -> str | None:
    return git(repo, "for-each-ref", "--format=%(objectname)", name).strip() or None


def commit(repo: Path, files: dict[str, str], message: str = "work") -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, newline="\n")
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


AGENTS = "ada/proj-agents"  # (ada/proj's agent repo)
WORKSPACE = "ada/crab-student-work"
MARKED = f"{github.MARK} for ada/proj"  # (the description crab-student gives the agent repo it makes)


def agent_push(tmp_path: Path, fake_github, branch: str, files: dict[str, str]) -> str:
    """A commit on `branch` of the agent repo, pushed the way an agent would."""
    work = tmp_path / "agent"
    if not work.exists():
        git(tmp_path, "clone", "-q", str(fake_github.bare(AGENTS)), "agent")
        git(work, "checkout", "-q", "-b", branch)
    sha = commit(work, files)
    git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return sha


def writes(fake_github) -> list:
    return [c for c in fake_github.calls if c[:2] == ("api", "-X") and c[2] != "GET"]


def packed(bare: Path) -> int:
    """The objects in a bare repo's packs (each push kept as a pack, duplicates too)."""
    counts = dict(line.split(": ") for line in git(bare, "count-objects", "-v").splitlines())
    return int(counts["in-pack"])


def state(repo: Path) -> dict:
    """What the user has in their repo: HEAD, the index, the working tree, refs (branches too) and stash."""
    files = {}
    for p in repo.rglob("*"):
        rel = p.relative_to(repo)
        if p.is_file() and rel.parts[0] != ".git":
            files[rel.as_posix()] = p.read_bytes()
    return {
        "head": git(repo, "symbolic-ref", "HEAD") + git(repo, "rev-parse", "HEAD"),
        "index": git(repo, "ls-files", "--stage"),
        "status": git(repo, "status", "--porcelain"),
        "files": files,
        "refs": git(repo, "for-each-ref"),
        "stash": git(repo, "stash", "list"),
    }


def checkout(repo: Path) -> dict:
    return {k: v for k, v in state(repo).items() if k != "refs"}


def record(user_repo: Path | None, branch: str = "agent/w1") -> dict:
    """An agent's record: on ada/proj, launched from the clone `user_repo`, or without a repo (None)."""
    repo = {"slug": AGENTS, "source": "ada/proj", "local_dir": str(user_repo)}
    if user_repo is None:
        repo = {"slug": WORKSPACE, "source": None}
    return {"name": branch.split("/")[1], "repo": {**repo, "branch": branch}}


@pytest.mark.parametrize(
    "url, slug",
    [("git@github.com:ada/proj.git", "ada/proj"), ("git@github.com:ada/proj", "ada/proj"),
     ("ssh://git@github.com/ada/my.proj", "ada/my.proj"), ("ssh://git@github.com/ada/proj.git", "ada/proj"),
     ("https://github.com/ada/proj.git\n", "ada/proj"), ("https://github.com/ada/proj/", "ada/proj"),
     ("https://gitlab.com/ada/proj.git", None), ("git@github.com:ada", None),
     ("https://github.com/ada/proj/tree/main", None), ("C:/github/ada/proj.git", None)],
)  # fmt: skip
def test_parse_remote(url, slug):
    assert github.parse_remote(url) == slug


def test_the_ssh_command_for_git_survives_a_space_in_the_home_path(tmp_path, monkeypatch):
    home = tmp_path / "Ada Lovelace"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    key = core.keys_dir() / "ada--proj"
    argv = shlex.split(github.ssh_command(key))  # (git runs it with sh, which splits it like this)
    assert Path(argv[argv.index("-i") + 1]) == key
    hosts = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
    (path,) = shlex.split(hosts)  # (ssh splits an option's value on spaces itself, minding quotes)
    assert Path(path.replace("~", str(home), 1)) == github.known_hosts_file()


def test_local_repo_finds_the_github_remote(user_repo):
    # git fetches from the fake through insteadOf; the URL the user configured is GitHub's
    assert github.parse_remote(git(user_repo, "remote", "get-url", "origin")) is None
    (user_repo / "sub").mkdir()
    r = github.local_repo(user_repo / "sub")
    assert (r.slug, r.head) == ("ada/proj", git(user_repo, "rev-parse", "HEAD").strip())
    assert r.top.samefile(user_repo)
    git(user_repo, "remote", "add", "aaa", "https://github.com/someone/fork.git")  # (listed before origin)
    assert github.local_repo(user_repo).slug == "ada/proj"
    git(user_repo, "remote", "set-url", "origin", "https://gitlab.com/ada/proj.git")
    assert github.local_repo(user_repo).slug == "someone/fork"


def test_local_repo_refuses_what_it_cannot_use(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    (tmp_path / "plain").mkdir()
    git(tmp_path, "init", "-q", "fresh")
    git(tmp_path / "fresh", "remote", "add", "origin", "git@github.com:ada/proj.git")
    git(tmp_path, "init", "-q", "elsewhere")
    commit(tmp_path / "elsewhere", {"a.txt": "a\n"})
    git(tmp_path / "elsewhere", "remote", "add", "origin", "https://gitlab.com/ada/proj.git")
    for name in ("plain", "fresh", "elsewhere"):  # not a repo, no commits, no GitHub remote
        assert error(github.local_repo, tmp_path / name).code == 2, name


def test_the_agent_repo_is_named_after_the_users_repo_on_the_users_own_account(fake_github):
    assert github.agent_repo("acme/model", "crab-student-work") == "ada/model-agents"  # (ada: the gh login)
    assert github.agent_repo(None, "crab-student-work") == WORKSPACE


def test_an_existing_agent_repo_must_be_one_crab_student_made_and_private(fake_github):
    assert github.check_agent_repo(AGENTS, "ada/proj", "agent/new") is False  # (made after the rent)
    bare = fake_github.create_repo(AGENTS, description=MARKED)
    assert github.check_agent_repo(AGENTS, "ada/proj", "agent/new") is True
    git(bare, "branch", "agent/taken", "main")
    assert error(github.check_agent_repo, AGENTS, "ada/proj", "agent/taken").code == 2
    fake_github.private[AGENTS] = False
    assert AGENTS in error(github.check_agent_repo, AGENTS, "ada/proj", "agent/new").message
    fake_github.private[AGENTS], fake_github.description[AGENTS] = True, "My agents"  # (a repo of the user's)
    fake_github.create_repo(WORKSPACE)  # (and the workspace's name taken by another)
    for slug, source in ((AGENTS, "ada/proj"), (WORKSPACE, None)):
        assert slug in error(github.check_agent_repo, slug, source, "agent/new").message
        assert slug in error(github.prepare_agent_repo, slug, source).message
    assert not writes(fake_github) and not fake_github.keys


@pytest.mark.parametrize("slug, source", [(AGENTS, "ada/proj"), (WORKSPACE, None)])
def test_an_agent_repo_is_made_once_private_and_fit_for_agents_with_one_deploy_key(slug, source, fake_github):
    key = github.prepare_agent_repo(slug, source)
    assert github.prepare_agent_repo(slug, source) == key and key.exists()
    assert sum(c[3] == "user/repos" for c in writes(fake_github)) == 1 and fake_github.private[slug]
    assert github.check_agent_repo(slug, source, "agent/w1")  # (fit for the next launch)
    (added,) = fake_github.keys[slug].values()
    assert added["key"].split() == Path(f"{key}.pub").read_text().split()[:2]
    assert added["read_only"] == "false"  # (agents push with it)


def test_a_deploy_key_github_no_longer_has_is_added_again(fake_github):
    key = github.prepare_agent_repo(AGENTS, "ada/proj")
    private = key.read_bytes()
    fake_github.keys[AGENTS].clear()  # (the user removed it on GitHub)
    assert github.prepare_agent_repo(AGENTS, "ada/proj") == key and key.read_bytes() == private
    (added,) = fake_github.keys[AGENTS].values()
    assert added["key"].split() == Path(f"{key}.pub").read_text().split()[:2]


def test_launches_preparing_one_agent_repo_at_the_same_time_make_it_and_its_key_once(
    fake_github, monkeypatch
):
    real = fake_github._api

    def slow(method, *a):  # (a slow GitHub: the other launches arrive meanwhile)
        if method == "POST":
            time.sleep(0.3)
        return real(method, *a)

    monkeypatch.setattr(fake_github, "_api", slow)
    with ThreadPoolExecutor(3) as pool:
        keys = set(pool.map(lambda _: github.prepare_agent_repo(AGENTS, "ada/proj"), range(3)))
    assert len(keys) == 1 and len(fake_github.keys[AGENTS]) == 1
    assert sum(c[3] == "user/repos" for c in writes(fake_github)) == 1


def test_snapshot_carries_changes_without_touching_the_users_repo(user_repo):
    commit(user_repo, {".gitignore": "*.log\n", "a.txt": "a\n", "b.txt": "b\n", "gone.txt": "g\n"}, "local")
    (user_repo / "README.md").write_text("stashed\n")
    git(user_repo, "stash", "-q")
    git(user_repo, "branch", "feature")
    for name in ("a.txt", "b.txt", "new.txt"):
        (user_repo / name).write_text(f"{name} staged\n")
    git(user_repo, "add", "a.txt", "b.txt", "new.txt")
    (user_repo / "b.txt").write_text("b staged, then changed again\n")
    (user_repo / "README.md").write_text("unstaged\n")
    (user_repo / "gone.txt").unlink()
    (user_repo / "notes.txt").write_text("untracked\n")
    (user_repo / "run.log").write_text("ignored\n")
    before = state(user_repo)
    repo = github.local_repo(user_repo)
    sha, carried, untracked, local = github.snapshot(repo)
    assert state(user_repo) == before
    assert git(user_repo, "rev-parse", f"{sha}^").strip() == repo.head
    tree = git(user_repo, "ls-tree", "-r", "--name-only", sha).split()
    assert sorted(tree) == [".gitignore", "README.md", "a.txt", "b.txt", "new.txt"]
    for name in tree:
        assert git(user_repo, "show", f"{sha}:{name}") == (user_repo / name).read_text()
    assert sorted(carried) == ["README.md", "a.txt", "b.txt", "gone.txt", "new.txt"]
    assert (untracked, local) == (["notes.txt"], 1)


def test_snapshot_without_changes_or_without_a_commit_is_head(user_repo):
    repo = github.local_repo(user_repo)
    assert github.snapshot(repo) == (repo.head, [], [], 0)
    (user_repo / "README.md").write_text("changed\n")
    objects = git(user_repo, "count-objects")
    assert github.snapshot(repo, commit=False) == (repo.head, ["README.md"], [], 0)
    assert git(user_repo, "count-objects") == objects  # (not even a blob was written)


def test_create_branch_pushes_the_starting_commit_to_the_agent_repo(user_repo, fake_github, tmp_path):
    fake_github.create_repo(AGENTS, description=MARKED)
    (user_repo / "README.md").write_text("changed\n")
    sha = github.snapshot(github.local_repo(user_repo))[0]
    before, theirs = state(user_repo), git(fake_github.bare("ada/proj"), "for-each-ref")
    github.create_branch({**record(user_repo, "agent/t1")["repo"], "start_sha": sha}, tmp_path / "t1.key")
    assert fake_github.branch_sha(AGENTS, "agent/t1") == sha
    assert state(user_repo) == before  # (no local branch or remote-tracking ref)
    assert git(fake_github.bare("ada/proj"), "for-each-ref") == theirs  # (the user's repo: never written to)


def test_only_the_first_push_to_an_agent_repo_sends_the_history(user_repo, fake_github, tmp_path):
    bare = fake_github.create_repo(AGENTS, description=MARKED)
    git(bare, "config", "receive.unpackLimit", "1")  # (every push is kept as a pack: count what each sent)
    for i in range(20):
        commit(user_repo, {f"f{i}.txt": f"{i}\n"}, f"c{i}")

    def push(branch: str) -> int:
        before, head = packed(bare), git(user_repo, "rev-parse", "HEAD").strip()
        github.create_branch({**record(user_repo, branch)["repo"], "start_sha": head}, tmp_path / "key")
        return packed(bare) - before

    assert push("agent/t1") > 60  # (the whole history)
    agent = tmp_path / "t1"  # (the agent pushes a commit the user's clone never sees)
    git(tmp_path, "clone", "-q", "-b", "agent/t1", str(bare), "t1")
    commit(agent, {"result.txt": "1\n"})
    git(agent, "push", "-q", "origin", "agent/t1")
    commit(user_repo, {"next.txt": "next\n"})
    assert push("agent/t2") < 10  # (the new commit, its tree and file, not the history again)


def test_a_branch_in_the_workspace_starts_from_its_default_branch(fake_github, tmp_path):
    github.prepare_agent_repo(WORKSPACE, None)
    head = github.default_head(WORKSPACE)
    github.create_branch({**record(None)["repo"], "start_sha": head}, tmp_path / "w1.key")
    assert fake_github.branch_sha(WORKSPACE, "agent/w1") == head


def test_create_branch_skips_the_users_own_pre_push_hook(user_repo, fake_github, tmp_path):
    fake_github.create_repo(AGENTS, description=MARKED)
    hook = user_repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n", newline="\n")
    hook.chmod(0o755)
    probe = subprocess.run(["git", "-C", str(user_repo), "push", "-q", "origin", "HEAD:refs/heads/probe"],
                           capture_output=True)  # fmt: skip
    if probe.returncode == 0:
        pytest.skip("git does not run hooks here")
    head = git(user_repo, "rev-parse", "HEAD").strip()
    github.create_branch({**record(user_repo, "agent/t1")["repo"], "start_sha": head}, tmp_path / "t1.key")
    assert fake_github.branch_sha(AGENTS, "agent/t1") == head


def test_read_file(fake_github, tmp_path):
    fake_github.create_repo(AGENTS, description=MARKED)
    agent_push(tmp_path, fake_github, "agent/t1", {"agent-runs/t1/REPORT.md": "# t1\nall good\n"})
    assert github.read_file(AGENTS, "agent/t1", "agent-runs/t1/REPORT.md") == "# t1\nall good\n"
    assert github.read_file(AGENTS, "agent/t1", "agent-runs/t2/REPORT.md") is None
    assert github.read_file(AGENTS, "agent/t2", "agent-runs/t1/REPORT.md") is None


@pytest.mark.parametrize("prefix, suffix", [("git@github.com:", ".git"), ("https://github.com/", ".git"),
                                            ("ssh://git@github.com/", "")])  # fmt: skip
def test_pull_adds_the_agent_repo_once_reached_like_the_clones_github_and_fetches_without_touching_the_checkout(
    prefix, suffix, user_repo, fake_github, tmp_path
):
    git(user_repo, "remote", "set-url", "origin", f"{prefix}ada/proj{suffix}")
    # (git reaches the fake through insteadOf; the URLs configured are GitHub's)
    git(user_repo, "config", f"url.{fake_github.root.as_posix()}/.insteadOf", prefix)
    fake_github.create_repo(AGENTS, description=MARKED)
    (user_repo / "README.md").write_text("my own edit\n")
    first = agent_push(tmp_path, fake_github, "agent/w1", {"result.txt": "1\n"})
    before = checkout(user_repo)
    assert "agent/w1" in github.pull(record(user_repo), None)  # (into the clone the agent started from)
    assert git(user_repo, "config", "--get", "remote.agents.url").strip() == f"{prefix}{AGENTS}{suffix}"
    assert ref(user_repo, "refs/remotes/agents/agent/w1") == first == ref(user_repo, "refs/heads/agent/w1")
    second = agent_push(tmp_path, fake_github, "agent/w1", {"result.txt": "2\n"})
    github.pull(record(user_repo), user_repo)
    assert ref(user_repo, "refs/heads/agent/w1") == second  # (fast-forwarded)
    assert sorted(git(user_repo, "remote").split()) == ["agents", "origin"] and checkout(user_repo) == before


def test_pull_needs_a_clone_with_a_github_remote_and_a_free_remote_name(
    user_repo, fake_github, tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    (tmp_path / "plain").mkdir()
    assert error(github.pull, record(user_repo), tmp_path / "plain").code == 2
    git(user_repo, "remote", "add", "agents", "https://github.com/someone/else.git")
    assert error(github.pull, record(user_repo), None).code == 2
    assert git(user_repo, "remote", "get-url", "agents").strip() == "https://github.com/someone/else.git"


@pytest.mark.parametrize("local", ["checked out", "diverged"])
def test_pull_leaves_a_busy_local_branch_alone(local, user_repo, fake_github, tmp_path):
    fake_github.create_repo(AGENTS, description=MARKED)
    agent_push(tmp_path, fake_github, "agent/w1", {"result.txt": "1\n"})
    github.pull(record(user_repo), None)
    git(user_repo, "switch", "-q", "agent/w1")
    if local == "diverged":
        git(user_repo, "commit", "-q", "--allow-empty", "-m", "mine")
        git(user_repo, "switch", "-q", "main")
    mine = ref(user_repo, "refs/heads/agent/w1")
    theirs = agent_push(tmp_path, fake_github, "agent/w1", {"result.txt": "2\n"})
    before = checkout(user_repo)
    github.pull(record(user_repo), None)
    assert ref(user_repo, "refs/remotes/agents/agent/w1") == theirs
    assert ref(user_repo, "refs/heads/agent/w1") == mine
    assert checkout(user_repo) == before


def test_a_workspace_pull_clones_it_first(fake_github, isolated):
    bare = fake_github.create_repo(WORKSPACE)
    git(bare, "branch", "agent/w1", "main")
    for _ in range(2):
        github.pull(record(None), None)
    clone = isolated / "crab-student-work"
    assert ref(clone, "refs/heads/agent/w1") == fake_github.branch_sha(WORKSPACE, "agent/w1")
    assert sum(call[:2] == ("repo", "clone") for call in fake_github.calls) == 1
    assert git(clone, "remote").split() == ["origin"]  # (its own remote is the workspace)
