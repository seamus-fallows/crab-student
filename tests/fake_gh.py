"""A fake GitHub: a folder of bare repos (real git for every clone, push and fetch) and an in-process
stand-in for the `gh` calls crab-student makes. Tests read `keys` and set `admin[slug]`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from crab_student.github import GhError


def git(cwd: Path | str, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


class FakeGitHub:
    def __init__(self, root: Path, login: str = "ada") -> None:
        self.root = root
        self.login = login
        self.admin: dict[str, bool] = {}
        self.keys: dict[str, dict[int, dict]] = {}
        self.calls: list[tuple[str, ...]] = []
        self._next_key = 100

    def bare(self, slug: str) -> Path:
        return self.root / f"{slug}.git"

    def create_repo(self, slug: str, readme: bool = True) -> Path:
        path = self.bare(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(path.parent, "init", "-q", "--bare", "-b", "main", path.name)
        if readme:
            seed = self.root / "_seed" / slug
            seed.mkdir(parents=True, exist_ok=True)
            git(seed, "init", "-q", "-b", "main")
            (seed / "README.md").write_text(f"# {slug}\n")
            git(seed, "add", "README.md")
            git(seed, "commit", "-q", "-m", "Initial commit")
            git(seed, "push", "-q", str(path), "main")
        return path

    def branch_sha(self, slug: str, branch: str) -> str | None:
        p = subprocess.run(["git", "-C", str(self.bare(slug)), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                           capture_output=True, text=True)  # fmt: skip
        return p.stdout.strip() or None

    # ---- gh -------------------------------------------------------------------------------------

    def gh(self, *args: str) -> str:
        self.calls.append(args)
        if args[:2] == ("repo", "clone"):
            git(".", "clone", "-q", str(self.bare(args[2])), args[3])
            return ""
        assert args[0] == "api", args
        rest = list(args[1:])
        method, raw = "GET", False
        if rest[:1] == ["-X"]:
            method, rest = rest[1], rest[2:]
        if rest[:1] == ["-H"]:
            raw, rest = "raw" in rest[1], rest[2:]
        path, fields = rest[0], dict(f.split("=", 1) for f in rest[1:] if not f.startswith("-"))
        return self._api(method, path, fields, raw)

    def _missing(self) -> GhError:
        return GhError("GitHub: gh: Not Found (HTTP 404)", True)

    def _api(self, method: str, path: str, fields: dict, raw: bool) -> str:
        parts = path.split("?")[0].split("/")
        if path == "user":
            return json.dumps({"login": self.login})
        if path == "user/repos" and method == "POST":
            self.create_repo(f"{self.login}/{fields['name']}")
            return json.dumps({"full_name": f"{self.login}/{fields['name']}"})
        slug = "/".join(parts[1:3])
        if not self.bare(slug).exists():
            raise self._missing()
        tail = parts[3:]
        if not tail:
            return json.dumps({"full_name": slug, "permissions": {"admin": self.admin.get(slug, True)}})
        if tail[:3] == ["git", "matching-refs", "heads"]:
            branch = "/".join(tail[3:])
            sha = self.branch_sha(slug, branch)
            return json.dumps([{"ref": f"refs/heads/{branch}", "object": {"sha": sha}}] if sha else [])
        if tail == ["commits", "HEAD"]:
            return json.dumps({"sha": git(self.bare(slug), "rev-parse", "HEAD").strip()})
        if tail == ["keys"] and method == "POST":
            self._next_key += 1
            self.keys.setdefault(slug, {})[self._next_key] = fields
            return json.dumps({"id": self._next_key})
        if tail[:1] == ["keys"] and method == "DELETE":
            if self.keys.get(slug, {}).pop(int(tail[1]), None) is None:
                raise self._missing()
            return ""
        if tail == ["git", "refs"] and method == "POST":
            git(self.bare(slug), "update-ref", fields["ref"], fields["sha"])
            return json.dumps({"ref": fields["ref"]})
        if tail[:3] == ["git", "refs", "heads"] and method == "DELETE":
            git(self.bare(slug), "update-ref", "-d", "refs/heads/" + "/".join(tail[3:]))
            return ""
        if tail[:1] == ["contents"] and raw:
            file, ref = "/".join(tail[1:]), path.split("?ref=")[1]
            p = subprocess.run(["git", "-C", str(self.bare(slug)), "show", f"{ref}:{file}"], capture_output=True,
                               text=True)  # fmt: skip
            if p.returncode:
                raise self._missing()
            return p.stdout
        raise AssertionError(f"fake gh: unhandled {method} {path}")
