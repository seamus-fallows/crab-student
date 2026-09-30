"""GitHub through `gh`, and git: the user's repo, the starting commit, deploy keys, branches, pull."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from crab_student import core

# github.com's ed25519 host key (https://api.github.com/meta), pinned for the workers' git.
HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_REMOTE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


class GhError(core.CrabError):
    def __init__(self, message: str, not_found: bool):
        super().__init__(message)
        self.not_found = not_found


@dataclass
class Repo:
    """The user's local repo."""

    top: Path
    slug: str
    head: str


def gh(*args: str) -> str:
    exe = core.tool("gh")
    if not exe:
        raise core.CrabError(
            "gh (GitHub's CLI) was not found: install it from https://cli.github.com, then run gh auth login."
        )
    proc = core.run([exe, *args], timeout=60, check=False)
    if proc.returncode != 0:
        err = core.last_line(proc.stderr or proc.stdout)
        raise GhError(f"GitHub: {err}", "HTTP 404" in (proc.stderr or ""))
    return proc.stdout


def api(path: str, *fields: str, method: str = "GET") -> object:
    out = gh("api", "-X", method, path, *fields)
    return json.loads(out) if out.strip() else None


def login() -> str:
    return str(api("user")["login"])


def git_url(slug: str) -> str:
    """Where git reaches a repo (tests point CRAB_STUDENT_GITHUB_URL at a folder of bare repos)."""
    base = os.environ.get("CRAB_STUDENT_GITHUB_URL")
    return f"{base.rstrip('/')}/{slug}.git" if base else f"git@github.com:{slug}.git"


def known_hosts_file() -> Path:
    return core.ssh_dir() / "github_known_hosts"


def ssh_command(key_file: Path) -> str:
    """GIT_SSH_COMMAND for git with a deploy key: no prompts, GitHub's pinned host key."""
    ssh = (core.tool("ssh") or "ssh").replace("\\", "/")
    key, hosts = str(key_file).replace("\\", "/"), str(known_hosts_file()).replace("\\", "/")
    return (
        f'"{ssh}" -i "{key}" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="{hosts}" '
        f"-o StrictHostKeyChecking=yes"
    )


def git(
    top: Path | str, *args: str, env: dict | None = None, check: bool = True, timeout: float = 120
) -> str:
    return core.run(["git", "-C", str(top), *args], env=env, check=check, timeout=timeout).stdout


def parse_remote(url: str) -> str | None:
    m = _REMOTE.match(url.strip())
    return m.group(1) if m else None


def local_repo(path: Path) -> Repo:
    p = core.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if p.returncode != 0:
        raise core.CrabError(
            f"{path} is not in a git repo. Run from your repo, pass --repo PATH, or use --no-repo.", 2
        )
    top = Path(p.stdout.strip())
    remotes = sorted(git(top, "remote").split(), key=lambda r: r != "origin")
    slug = next(
        (
            s
            for r in remotes
            if (s := parse_remote(git(top, "config", "--get", f"remote.{r}.url", check=False)))
        ),
        None,
    )
    if not slug:
        raise core.CrabError(
            f"{top} has no GitHub remote. Add one, or use --no-repo for the workspace repo.", 2
        )
    head = git(top, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    if not head:
        raise core.CrabError(f"{top} has no commits yet. Commit something first, or use --no-repo.", 2)
    return Repo(top, slug, head)


def branch_sha(slug: str, branch: str) -> str | None:
    refs = api(f"repos/{slug}/git/matching-refs/heads/{branch}") or []
    return next((r["object"]["sha"] for r in refs if r.get("ref") == f"refs/heads/{branch}"), None)


def check_repo(slug: str, branch: str) -> None:
    try:
        info = api(f"repos/{slug}")
    except GhError as e:
        if e.not_found:
            raise core.CrabError(
                f"GitHub cannot find {slug}, or you cannot see it (gh auth status shows who you are)."
            ) from None
        raise
    if not (info.get("permissions") or {}).get("admin"):
        raise core.CrabError(
            f"you need admin rights on {slug} so crab-student can add a deploy key for the worker. "
            "Ask an owner, or run the task in your workspace repo with --no-repo."
        )
    if branch_sha(slug, branch):
        raise core.CrabError(f"branch {branch} already exists on {slug}. Pick another name.", 2)


def snapshot(repo: Repo, commit: bool = True) -> tuple[str, list[str], list[str], int]:
    """The worker's starting commit: HEAD, plus the uncommitted changes to tracked files (what `git commit -a`
    would commit) as one commit made in a copy of the index (the user's index, working tree, branches and
    stash are untouched). Returns (start sha, files carried, untracked files not carried, local commits not
    on any remote)."""
    top = repo.top
    changed = [f for f in git(top, "diff", "--name-only", "-z", "HEAD").split("\0") if f]
    untracked = [f for f in git(top, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if f]
    local = int(git(top, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() or 0)
    if not changed or not commit:
        return repo.head, changed, untracked, local
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"  # (a copy of the user's, so files staged but never committed come too)
        shutil.copyfile(top / git(top, "rev-parse", "--git-path", "index").strip(), index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git(top, "add", "-u", env=env)
        tree = git(top, "write-tree", env=env).strip()
    env = dict(os.environ)
    if not git(top, "config", "user.email", check=False).strip():
        for who in ("AUTHOR", "COMMITTER"):
            env |= {
                f"GIT_{who}_NAME": "crab-student",
                f"GIT_{who}_EMAIL": "crab-student@users.noreply.github.com",
            }
    sha = git(
        top,
        "commit-tree",
        tree,
        "-p",
        repo.head,
        "-m",
        "crab-student: uncommitted changes at launch",
        env=env,
    ).strip()
    return sha, changed, untracked, local


def check_workspace(slug: str, branch: str) -> bool:
    """Before renting: whether the workspace repo exists; an existing one must be private and without the
    branch."""
    try:
        info = api(f"repos/{slug}")
    except GhError as e:
        if e.not_found:
            return False
        raise
    if not info.get("private"):
        raise core.CrabError(f"{slug} is public, so crab-student will not put workers' work there. Make it "
                             "private, or choose another name: crab-student config workspace_repo NAME")  # fmt: skip
    if branch_sha(slug, branch):
        raise core.CrabError(f"branch {branch} already exists on {slug}. Pick another name.", 2)
    return True


def ensure_workspace(user: str, name: str) -> str:
    """The private workspace repo, created if missing (check_workspace vetted an existing one)."""
    slug = f"{user}/{name}"
    try:
        api(f"repos/{slug}")
    except GhError as e:
        if not e.not_found:
            raise
        api("user/repos", "-f", f"name={name}", "-F", "private=true", "-F", "auto_init=true", method="POST")
    return slug


def default_head(slug: str) -> str:
    for attempt in range(5):  # (a repo created a moment ago may not have its first commit yet)
        try:
            return str(api(f"repos/{slug}/commits/HEAD")["sha"])
        except GhError:
            if attempt == 4:
                raise
            time.sleep(2)
    raise AssertionError


def add_deploy_key(slug: str, title: str, public_key: str) -> int:
    data = api(
        f"repos/{slug}/keys",
        "-f",
        f"title={title}",
        "-f",
        f"key={public_key.strip()}",
        "-F",
        "read_only=false",
        method="POST",
    )
    return int(data["id"])


def remove_deploy_key(slug: str, key_id: int) -> None:
    try:
        api(f"repos/{slug}/keys/{key_id}", method="DELETE")
    except GhError as e:
        if not e.not_found:
            raise


def create_branch(repo: dict, key_file: Path) -> None:
    """Put the worker's branch on GitHub: the starting commit pushed from the user's repo with the worker's
    deploy key (no user credentials, so no prompt can block), or, for the workspace, made through the API."""
    if repo["workspace"]:
        api(
            f"repos/{repo['slug']}/git/refs",
            "-f",
            f"ref=refs/heads/{repo['branch']}",
            "-f",
            f"sha={repo['start_sha']}",
            method="POST",
        )
        return
    env = {**os.environ, "GIT_SSH_COMMAND": ssh_command(key_file)}
    for attempt in range(3):  # (a new deploy key can take a moment to work)
        p = core.run(
            [
                "git",
                "-C",
                repo["local_dir"],
                "push",
                "-q",
                "--no-verify",  # (the user's own pre-push hooks are for their pushes, not this one)
                git_url(repo["slug"]),
                f"{repo['start_sha']}:refs/heads/{repo['branch']}",
            ],
            env=env,
            check=False,
            timeout=180,
        )
        if p.returncode == 0:
            return
        if attempt < 2:
            time.sleep(3)
    raise core.CrabError(f"could not push the starting point to {repo['slug']}: {core.last_line(p.stderr)}")


def delete_branch_if_at(slug: str, branch: str, sha: str) -> bool:
    """Delete a branch only while it still points at `sha` (nobody worked on it)."""
    if branch_sha(slug, branch) != sha:
        return False
    api(f"repos/{slug}/git/refs/heads/{branch}", method="DELETE")
    return True


def read_report(slug: str, branch: str, path: str) -> str | None:
    try:
        return gh(
            "api", "-H", "Accept: application/vnd.github.raw", f"repos/{slug}/contents/{path}?ref={branch}"
        )
    except GhError as e:
        if e.not_found:
            return None
        raise


def pull(rec: dict, path: Path | None) -> str:
    """Fetch the worker's branch without touching the checkout: the remote-tracking ref always, and a local
    branch of the same name when that is a fast-forward."""
    repo, branch = rec["repo"], rec["repo"]["branch"]
    top = Path(path or Path.cwd())
    if repo["workspace"] and path is None:  # (the workspace repo has a home of its own)
        top = Path.home() / repo["slug"].split("/")[1]
        if not (top / ".git").exists():
            gh("repo", "clone", repo["slug"], str(top))
    top = Path(git(top, "rev-parse", "--show-toplevel", check=False).strip() or top)
    remote = next(
        (
            r
            for r in git(top, "remote", check=False).split()
            if parse_remote(git(top, "config", "--get", f"remote.{r}.url", check=False)) == repo["slug"]
        ),
        None,
    )
    if not remote:
        raise core.CrabError(
            f"{top} has no remote for {repo['slug']}. Run pull from a clone of it, or pass --repo PATH.", 2
        )
    git(top, "fetch", "-q", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}", timeout=300)
    sha = git(top, "rev-parse", "--short", f"{remote}/{branch}").strip()
    local = core.run(
        ["git", "-C", str(top), "fetch", "-q", remote, f"{branch}:{branch}"], check=False, timeout=300
    )
    base = git(top, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or "HEAD"
    note = (
        f"Look: git log {base}..{branch}  - try: git switch {branch}"
        if local.returncode == 0
        else f"Your local {branch} was left as it is (checked out or changed); the fetched work is {remote}/{branch}."
    )
    return f"Fetched {branch} ({sha}) into {top}.\n{note}"
