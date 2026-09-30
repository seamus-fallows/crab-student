"""github.py against the fake GitHub: real git on bare repos, `gh` answered in-process."""

from __future__ import annotations

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


def worker_push(tmp_path: Path, fake_github, branch: str, files: dict[str, str]) -> str:
    """A commit on `branch` of ada/proj, pushed the way a worker would."""
    work = tmp_path / "worker"
    if not work.exists():
        git(tmp_path, "clone", "-q", str(fake_github.bare("ada/proj")), "worker")
        git(work, "checkout", "-q", "-b", branch)
    sha = commit(work, files)
    git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return sha


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


def record(branch: str = "agent/w1", slug: str = "ada/proj", workspace: bool = False) -> dict:
    return {"name": branch.split("/")[1], "repo": {"slug": slug, "branch": branch, "workspace": workspace}}


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


def test_check_repo(fake_github):
    bare = fake_github.create_repo("ada/proj")
    github.check_repo("ada/proj", "agent/new")
    git(bare, "branch", "agent/taken", "main")
    assert error(github.check_repo, "ada/proj", "agent/taken").code == 2
    fake_github.admin["ada/proj"] = False  # (a deploy key needs admin rights)
    assert error(github.check_repo, "ada/proj", "agent/new").code == 1
    missing = error(github.check_repo, "ada/nope", "agent/new")
    assert missing.code == 1 and "ada/nope" in missing.message


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


def test_create_branch_pushes_the_starting_commit(user_repo, fake_github, tmp_path):
    (user_repo / "README.md").write_text("changed\n")
    sha = github.snapshot(github.local_repo(user_repo))[0]
    before = state(user_repo)
    repo = {"workspace": False, "slug": "ada/proj", "branch": "agent/t1", "start_sha": sha}
    github.create_branch({**repo, "local_dir": str(user_repo)}, tmp_path / "t1.key")
    assert fake_github.branch_sha("ada/proj", "agent/t1") == sha
    assert state(user_repo) == before  # (no local branch or remote-tracking ref)


def test_the_workspace_is_created_once_and_branched_through_the_api(fake_github, tmp_path):
    slug = github.ensure_workspace("ada", "crab-student-work")
    assert slug == "ada/crab-student-work" and fake_github.bare(slug).exists()
    assert github.ensure_workspace("ada", "crab-student-work") == slug
    assert sum("user/repos" in call for call in fake_github.calls) == 1
    head = github.default_head(slug)
    repo = {"workspace": True, "slug": slug, "branch": "agent/w1", "start_sha": head}
    github.create_branch(repo, tmp_path / "w1.key")
    assert fake_github.branch_sha(slug, "agent/w1") == head


def test_deploy_keys(fake_github):
    fake_github.create_repo("ada/proj")
    kid = github.add_deploy_key("ada/proj", "crab-student t1", "ssh-ed25519 AAAA t1\n")
    key = fake_github.keys["ada/proj"][kid]
    assert key == {"title": "crab-student t1", "key": "ssh-ed25519 AAAA t1", "read_only": "false"}
    github.remove_deploy_key("ada/proj", kid)
    assert fake_github.keys["ada/proj"] == {}
    github.remove_deploy_key("ada/proj", kid)  # (already gone: GitHub answers 404)


def test_a_branch_is_deleted_only_while_nobody_worked_on_it(fake_github, tmp_path):
    bare = fake_github.create_repo("ada/proj")
    start = fake_github.branch_sha("ada/proj", "main")
    git(bare, "branch", "agent/t1", "main")
    assert github.delete_branch_if_at("ada/proj", "agent/t1", start)
    assert fake_github.branch_sha("ada/proj", "agent/t1") is None
    assert not github.delete_branch_if_at("ada/proj", "agent/t1", start)
    worker_push(tmp_path, fake_github, "agent/t2", {"result.txt": "1\n"})
    assert not github.delete_branch_if_at("ada/proj", "agent/t2", start)
    assert fake_github.branch_sha("ada/proj", "agent/t2")


def test_read_report(fake_github, tmp_path):
    fake_github.create_repo("ada/proj")
    worker_push(tmp_path, fake_github, "agent/t1", {"agent-runs/t1/REPORT.md": "# t1\nall good\n"})
    assert github.read_report("ada/proj", "agent/t1", "agent-runs/t1/REPORT.md") == "# t1\nall good\n"
    assert github.read_report("ada/proj", "agent/t1", "agent-runs/t2/REPORT.md") is None
    assert github.read_report("ada/proj", "agent/t2", "agent-runs/t1/REPORT.md") is None


def test_pull_fetches_without_touching_the_checkout(user_repo, fake_github, tmp_path):
    (user_repo / "README.md").write_text("my own edit\n")
    first = worker_push(tmp_path, fake_github, "agent/w1", {"result.txt": "1\n"})
    before = checkout(user_repo)
    assert "agent/w1" in github.pull(record(), user_repo)
    assert ref(user_repo, "refs/remotes/origin/agent/w1") == first == ref(user_repo, "refs/heads/agent/w1")
    second = worker_push(tmp_path, fake_github, "agent/w1", {"result.txt": "2\n"})
    github.pull(record(), user_repo)
    assert ref(user_repo, "refs/heads/agent/w1") == second  # (fast-forwarded)
    assert checkout(user_repo) == before
    assert error(github.pull, record(slug="ada/other"), user_repo).code == 2  # (no remote for that repo)


@pytest.mark.parametrize("local", ["checked out", "diverged"])
def test_pull_leaves_a_busy_local_branch_alone(local, user_repo, fake_github, tmp_path):
    worker_push(tmp_path, fake_github, "agent/w1", {"result.txt": "1\n"})
    github.pull(record(), user_repo)
    git(user_repo, "switch", "-q", "agent/w1")
    if local == "diverged":
        git(user_repo, "commit", "-q", "--allow-empty", "-m", "mine")
        git(user_repo, "switch", "-q", "main")
    mine = ref(user_repo, "refs/heads/agent/w1")
    theirs = worker_push(tmp_path, fake_github, "agent/w1", {"result.txt": "2\n"})
    before = checkout(user_repo)
    github.pull(record(), user_repo)
    assert ref(user_repo, "refs/remotes/origin/agent/w1") == theirs
    assert ref(user_repo, "refs/heads/agent/w1") == mine
    assert checkout(user_repo) == before


def test_a_workspace_pull_clones_it_first(fake_github, isolated):
    bare = fake_github.create_repo("ada/crab-student-work")
    git(bare, "branch", "agent/w1", "main")
    for _ in range(2):
        github.pull(record(slug="ada/crab-student-work", workspace=True), None)
    clone = isolated / "crab-student-work"
    assert ref(clone, "refs/heads/agent/w1") == fake_github.branch_sha("ada/crab-student-work", "agent/w1")
    assert sum(call[:2] == ("repo", "clone") for call in fake_github.calls) == 1
