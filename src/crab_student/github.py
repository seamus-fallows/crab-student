"""GitHub through `gh`, and git: the user's repo and the starting commit, agent repos and their deploy keys, the
agent's branch, pull."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from crab_student import core

# github.com's ed25519 host key (https://api.github.com/meta), pinned for the agents' git.
HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
_REMOTE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)
# The start of an agent repo's description: crab-student writes only to repos it made for agents' branches.
MARK = "crab-student agent branches"
KEY_TITLE = "crab-student"  # (agent repos' deploy keys)


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


def ssh_command(key_path: Path) -> str:
    """GIT_SSH_COMMAND for git with a deploy key: no prompts, GitHub's pinned host key. git runs it with sh, so every
    argument is quoted for sh; ssh splits an option's value on spaces itself, so the known-hosts path is one ssh
    reads whole (core.ssh_path)."""
    ssh = (core.tool("ssh") or "ssh").replace("\\", "/")
    hosts = f"UserKnownHostsFile={core.ssh_path(known_hosts_file())}"
    return shlex.join(
        [ssh, "-i", str(key_path).replace("\\", "/"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
         "-o", hosts, "-o", "StrictHostKeyChecking=yes"]
    )  # fmt: skip


def git(
    top: Path | str, *args: str, env: dict | None = None, check: bool = True, timeout: float = 120
) -> str:
    return core.run(["git", "-C", str(top), *args], env=env, check=check, timeout=timeout).stdout


def parse_remote(url: str) -> str | None:
    m = _REMOTE.match(url.strip())
    return m.group(1) if m else None


def remotes(top: Path) -> dict[str, str]:
    """A clone's remotes and their URLs, origin first."""
    names = sorted(git(top, "remote", check=False).split(), key=lambda r: r != "origin")
    return {r: git(top, "config", "--get", f"remote.{r}.url", check=False).strip() for r in names}


def local_repo(path: Path) -> Repo:
    p = core.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if p.returncode != 0:
        raise core.CrabError(
            f"{path} is not in a git repo. Run from your repo, pass --repo PATH, or use --no-repo.", 2
        )
    top = Path(p.stdout.strip())
    slug = next((s for url in remotes(top).values() if (s := parse_remote(url))), None)
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


def snapshot(repo: Repo, commit: bool = True) -> tuple[str, list[str], list[str], int]:
    """The agent's starting commit: HEAD, plus the uncommitted changes to tracked files (what `git commit -a`
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
        "Uncommitted changes at launch",
        env=env,
    ).strip()
    return sha, changed, untracked, local


# ---- agent repos ---------------------------------------------------------------------------------
# Agents never get access to the user's repo. An agent's branch lives in its agent repo, a private repo on the
# user's own account: `<repo name>-agents` for the user's repo (two repos with one name share it: it only holds
# agents' branches), or the workspace for a task without a repo. One deploy key per agent repo, kept.


def agent_repo(source: str | None, workspace: str) -> str:
    """The agent repo for `source`, the user's repo (None: a task without a repo, in the workspace repo)."""
    return f"{login()}/{source.split('/')[1] + '-agents' if source else workspace}"


def _exists(slug: str, source: str | None) -> bool:
    """Whether the agent repo exists. An existing one must carry the mark and be private: a repo of the user's that
    happens to have its name is never written to."""
    try:
        info = api(f"repos/{slug}")
    except GhError as e:
        if e.not_found:
            return False
        raise
    if not str(info.get("description") or "").startswith(MARK):
        fix = "rename it on GitHub, or use --no-repo"
        if source is None:
            fix = "choose another name: crab-student config workspace_repo NAME"
        raise core.CrabError(
            f"{slug} exists, but crab-student did not make it for agents' branches (its description does not start "
            f"with '{MARK}'), so it will not put agents' work there: {fix}."
        )
    if not info.get("private"):
        raise core.CrabError(
            f"{slug} is public, so crab-student will not put agents' work there: make it private."
        )
    return True


def check_agent_repo(slug: str, source: str | None, branch: str) -> bool:
    """Before renting (nothing is written): whether the agent repo exists. An existing one must be fit for agents'
    work and not have the branch yet."""
    exists = _exists(slug, source)
    if exists and branch_sha(slug, branch):
        raise core.CrabError(f"branch {branch} already exists on {slug}. Pick another name.", 2)
    return exists


def prepare_agent_repo(slug: str, source: str | None) -> Path:
    """The agent repo, made private and marked the first time (with a first commit: the workspace's branches start
    from it, and GitHub's API answers differently for an empty repo), and its deploy key: made the first time and
    kept, and added to the repo whenever GitHub does not have it (the user may remove it). Parallel launches (a
    sweep) wait under the repo's lock. Returns the key's path."""
    with core.locked(f"_repo-{slug.replace('/', '--')}", wait=300):
        if not _exists(slug, source):
            api("user/repos", "-f", f"name={slug.split('/')[1]}", "-f",
                f"description={MARK} for {source or 'tasks without a repo'}", "-F", "private=true",
                "-F", "auto_init=true", method="POST")  # fmt: skip
        path = key_file(slug)
        if not path.exists():
            core.keygen(path, KEY_TITLE)
        public = Path(f"{path}.pub").read_text(encoding="utf-8").split()[:2]  # (type and key: no comment)
        if not any(k["key"].split()[:2] == public for k in api(f"repos/{slug}/keys?per_page=100") or []):
            api(f"repos/{slug}/keys", "-f", f"title={KEY_TITLE}", "-f", f"key={' '.join(public)}",
                "-F", "read_only=false", method="POST")  # fmt: skip
    return path


def key_file(slug: str) -> Path:
    """An agent repo's deploy key (its public half beside it, .pub)."""
    return core.keys_dir() / slug.replace("/", "--")


def default_head(slug: str) -> str:
    for attempt in range(5):  # (a repo created a moment ago may not have its first commit yet)
        try:
            return str(api(f"repos/{slug}/commits/HEAD")["sha"])
        except GhError:
            if attempt == 4:
                raise
            time.sleep(2)
    raise AssertionError


def create_branch(repo: dict, key_path: Path) -> None:
    """Put the agent's branch on its agent repo: the starting commit pushed from the user's clone with the repo's
    deploy key (no user credentials, so no prompt can block), or, for the workspace, made through the API. The
    push first works out what the agent repo already has (its branches' tips are agents' commits this clone never
    saw), so only the first push sends the whole history."""
    if repo["source"] is None:
        api(
            f"repos/{repo['slug']}/git/refs",
            "-f",
            f"ref=refs/heads/{repo['branch']}",
            "-f",
            f"sha={repo['start_sha']}",
            method="POST",
        )
        return
    env = {**os.environ, "GIT_SSH_COMMAND": ssh_command(key_path)}
    for attempt in range(3):  # (a new deploy key can take a moment to work)
        p = core.run(
            [
                "git",
                "-C",
                repo["local_dir"],
                "-c",
                "push.negotiate=true",
                "push",
                "-q",
                "--no-verify",  # (the user's own pre-push hooks are for their pushes, not this one)
                git_url(repo["slug"]),
                f"{repo['start_sha']}:refs/heads/{repo['branch']}",
            ],
            env=env,
            check=False,
            timeout=1800,  # (the first push sends the whole history)
        )
        if p.returncode == 0:
            return
        if attempt < 2:
            time.sleep(3)
    raise core.CrabError(f"could not push the starting point to {repo['slug']}: {core.last_line(p.stderr)}")


def read_file(slug: str, branch: str, path: str) -> str | None:
    try:
        return gh(
            "api", "-H", "Accept: application/vnd.github.raw", f"repos/{slug}/contents/{path}?ref={branch}"
        )
    except GhError as e:
        if e.not_found:
            return None
        raise


def pull(rec: dict, path: Path | None) -> str:
    """Fetch the agent's branch from its agent repo without touching the checkout: the remote-tracking ref always,
    and a local branch of the same name when that is a fast-forward. Into the clone at `path`, else the one the
    agent started from (the workspace has a clone of its own, made the first time). A clone without a remote for
    the agent repo gets one, `agents`, reached the way the clone reaches GitHub (https or ssh), so the user's own
    credentials work."""
    repo, branch = rec["repo"], rec["repo"]["branch"]
    if path:
        top = Path(path)
    elif repo["source"]:
        top = Path(repo["local_dir"])
    else:
        top = Path.home() / repo["slug"].split("/")[1]
        if not (top / ".git").exists():
            gh("repo", "clone", repo["slug"], str(top))
    top = Path(git(top, "rev-parse", "--show-toplevel", check=False).strip() or top)
    urls, added = remotes(top), ""
    remote = next((r for r, url in urls.items() if parse_remote(url) == repo["slug"]), None)
    if not remote:
        template = next((url for url in urls.values() if parse_remote(url)), None)
        if not template:
            raise core.CrabError(
                f"{top} is not a clone of a GitHub repo. Give yours: crab-student pull {rec['name']} --repo PATH",
                2,
            )
        if "agents" in urls:
            raise core.CrabError(
                f"{top} has a remote named agents for another repo: rename it (git remote rename agents OTHER), "
                "then pull again.",
                2,
            )
        m = _REMOTE.match(template)
        git(top, "remote", "add", "agents", template[: m.start(1)] + repo["slug"] + template[m.end(1) :])
        remote, added = "agents", f"Added the remote agents ({repo['slug']}) to {top}.\n"
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
    return f"{added}Fetched {branch} ({sha}) into {top}.\n{note}"
