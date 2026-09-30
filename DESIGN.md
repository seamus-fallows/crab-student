# crab-student: design

This is the design for the requirements in `REQUIREMENTS.md`. It is meant to be built exactly as written.
Where the requirements leave a choice, the choice is made here and listed with its reason in section 5.
Section 9 lists the places where outside behaviour is uncertain and how the design limits the damage.

Contents:

1. Architecture overview
2. Package layout and line budget
3. Data formats
4. Key algorithms and rules
5. Decisions
6. Claude Code integration
7. Errors, exit codes and terminal output
8. Test plan
9. Risks and uncertainties

---

## 1. Architecture overview

### 1.1 The idea in one paragraph

There are two sides and no service. The **local side** is a Python CLI (`crab-student`). Each
command runs, does its work and exits. Records live in JSON files under `~/.crab-student/`, and each
record has a lock file. The CLI talks to Vast over its REST API (urllib), to GitHub through the `gh`
CLI and `git`, and to machines through the system `ssh` binary. Every machine command is one
`ssh <alias> <python> /root/crab-student/cs.py <command>` call that prints one JSON object. The
**machine side** is one standard-library Python script, `cs.py`, plus a short `onstart` shell command
that Vast runs at every container start. On the machine, one long-running process (`cs.py watch`,
the **watchdog**) owns the machine. It keeps the worker's Claude Code alive in a tmux session called
`worker`, measures activity, enforces the idle and time limits, saves work, sends notifications and
stops the machine. A dead-man loop started directly by `onstart` stops the machine if the watchdog
never starts or dies. The user watches and steers the worker by attaching to its tmux session over
ssh (`crab-student open`).

```
 user's computer                                          rented Vast machine (root)
 ----------------                                         ---------------------------------------
 Claude Code  --runs-->  crab-student CLI  --REST-->  Vast API  --creates/starts/stops-->  container
   (skill,               (one process per                                                    |
    launcher)             command, no daemon)  --ssh (system ssh, one JSON per call)-->  cs.py <cmd>
                              |                                                              |
                              +--gh / git (deploy key)-->  GitHub  <--git push (deploy key)--+
 user's terminal  --crab-student open-->  ssh -t ... tmux attach -t worker   (Claude Code TUI)
                                                                             |
                                          onstart: vast.env, dead-man loop, cs.py watch (watchdog)
                                          watchdog: tmux "worker" -> cs.py claude -> claude ...
```

### 1.2 What runs where, and when

| Where | Process | Lifetime | Job |
|---|---|---|---|
| local | `crab-student <cmd>` | one command | Everything local: planning, renting, uploads, views, lifecycle |
| local | the launcher (a background Claude Code subagent) | one launch | Follows `crab-student guide launch`: estimates, writes the brief, runs `crab-student launch` |
| machine | `onstart` (Vast runs it as root at every container start) | seconds | Saves `CONTAINER_ID`/`CONTAINER_API_KEY` to `vast.env` (0600), writes `booted`, starts the dead-man loop, starts the watchdog if setup was done |
| machine | dead-man loop (bash, started by onstart) | container life | Every 60 s: if neither `booted` nor `heartbeat` is newer than 45 min, calls Vast stop |
| machine | `cs.py setup` (run over ssh by the launch) | about 30-45 s | One-time setup in five steps; ends by starting the watchdog |
| machine | `cs.py watch` (the watchdog, one instance, flock) | container life | Every 60 s: heartbeat, keep tmux/Claude alive, measure activity, used time, nudges, notifications, idle and time-limit stops |
| machine | tmux session `worker` running `cs.py claude` | container life | Runs `claude` (or the fake) with the right environment and resumes the conversation |
| machine | `cs.py status/save/check/set-limit/send/attach` | one ssh call | What the local side asks for |
| machine | `crab-status` (= `cs.py declare`) | one call | The worker declares its state and one-line message |

Nothing runs locally between commands. The only local "watching" is `crab-student list --watch`,
which is a foreground loop the user starts and stops.

### 1.3 How the two sides talk

- **Vast REST** (local only, with the user's key): search, rent, instance list and lookups, start,
  stop, destroy, add an SSH key. The machine uses its instance-scoped `CONTAINER_API_KEY` for one
  call only: stop itself.
- **SSH** (local to machine, system `ssh` with our own config file, key-only, `BatchMode=yes`):
  - the upload and setup: one call with a tar.gz stream on stdin; progress lines come back on stdout;
  - one-shot commands: `cs.py <command>` prints exactly one JSON object as its last stdout line;
  - interactive: `ssh -t <alias> crab-attach` (a script setup installs; it execs `tmux attach -t worker`).
- **GitHub**: the local side uses `gh` (login, repo checks, deploy keys, branch refs, reading
  `REPORT.md`) and `git` (the launch pushes the starting point with the deploy key, `pull` fetches).
  The machine uses only `git` with its deploy key.
- **Notifications**: the machine POSTs to `notify_url` directly.

There is no message queue and no service. Each piece of state has exactly one writer:

| State | Writer |
|---|---|
| local record `workers/<name>.json` | the CLI command holding that record's lock |
| machine `config.json` | the local side (upload, `set-limit`) |
| machine `state.json` | the watchdog |
| machine `worker.json` | `crab-status` (the worker) |
| the git branch `agent/<name>` | the local launch (first commit only), then the machine |

### 1.4 Launch timeline (typical)

```
t=0s     launcher: crab-student launch NAME --hours 3 --gpus 1 --gpu-ram 24 ... --json <<'BRIEF' ... BRIEF
t=0-2s   validate the name; check init; repo checks with gh (admin rights, branch absent);
         snapshot of uncommitted tracked changes (a local commit object, the checkout is untouched)
t=2-4s   offer search (POST /bundles/), filter, rank, pick primary + 2 fallbacks; budget; approval gate
         (if over a limit: record "pending", print the approval line, exit 3 - nothing rented)
t=4s     record phase=renting + label written BEFORE the request; PUT /asks/<offer>/ -> new_contract
t=5-9s   (the image pull starts on Vast) deploy key created and added with gh; the starting point
         pushed to agent/NAME with that key
t=9s-3m  poll the instance every 5 s, printing status_msg changes ("pulling image 43%");
         when actual_status=running: add our SSH key, wait until ssh answers
t+0-45s  one ssh call: tar.gz upload -> cs.py setup (5 steps; Claude Code install runs in
         parallel with the clone); setup starts the watchdog; the watchdog starts tmux + claude;
         setup waits until the conversation file appears
t+45s    record phase=ready; ssh config entry written; result line printed (and JSON with --json)
```

With a host that has the image cached, the whole launch takes about 1-3 minutes, and the
upload and setup take well under a minute. If the offer is taken (4xx), the launch moves to the next
one at once. If it has not booted after 15 minutes or reports a boot error, the launch destroys it
(it holds nothing) and rents the next fallback.

### 1.5 Stop timelines

Idle stop or time-limit stop (on the machine):

```
every 60 s   watchdog tick: used_seconds += elapsed; activity = transcript grew | container CPU
             >= 0.25 core | any GPU >= 10 %; last_activity updated
idle/2       worker said needs-you and nothing happened: the watchdog types a "decide yourself" nudge
limit-10min  the watchdog types a "10 minutes left: commit, push, update REPORT.md" warning
stop rule    now - last_activity >= idle_minutes  or  used >= limit (after a 5-minute boot grace)
  -> state.stopping = reason; notify "NAME stopped (reason)"
  -> save: git add -u + small untracked files, commit "crab-student: autosave (reason)", push
  -> PUT /api/v0/instances/$CONTAINER_ID/ {"state":"stopped"} with CONTAINER_API_KEY
     (retried at every tick until the container is gone)
```

User stop (local): `crab-student stop NAME`, then `ssh cs.py save` (up to 3 min; it reports what it
committed and pushed), then Vast `PUT {"state":"stopped"}` with the user's key. The record's `last_seen`
is updated with reason `user`. If ssh fails, the command warns ("could not save first: ...;
the disk is kept, nothing is lost") and stops anyway.

Dead-man stop (machine): the watchdog's heartbeat file is older than 45 minutes (or it never
existed and boot was 45 minutes ago). The onstart loop then calls Vast stop with no save, because
the machinery that would save is the thing that is broken.

Restart: `crab-student start NAME` sets Vast `state: running`. Vast reruns `onstart`, which gives a new
`booted`, a new dead-man loop, and starts the watchdog. The watchdog starts tmux, `cs.py claude`
finds the saved conversation and runs `claude --resume <session-id> "<restart note>"`. The local
side waits for ssh, pushes the current time limit (`cs.py set-limit`) and rewrites the ssh config
entry, because Vast may have changed the port.

---

## 2. Package layout and line budget

```
pyproject.toml            # name crab-student, Python >=3.11, deps: keyring; script crab-student = crab_student.cli:main
README.md  CLAUDE.md  SPEC.md
src/crab_student/
  __init__.py  __main__.py          # version; `python -m crab_student`
  cli.py        core.py      vast.py      offers.py    github.py
  machine.py    launch.py    workers.py   setup.py
  data/SKILL.md  data/launch.md     # the skill and the launcher's guide (package data)
  remote/cs.py  remote/fake_claude.py  remote/worker.md   # uploaded flat to /root/crab-student/
tests/  (section 8)
```

There are nine local modules and two machine scripts. The only runtime dependency is `keyring`.
External programs: `ssh`, `ssh-keygen`, `git`, `gh`; optional ones are `wt` (Windows, for `open --all`),
`tmux` (macOS/Linux, for `open --all`) and `code` (for `--vscode`).

Conventions for all local code:

- Paths are computed at call time from `Path.home()` (tests point `HOME`/`USERPROFILE` at a
  temporary folder).
- Errors are raised as `core.CrabError(message, code)`. `cli.main` prints `error: <message>` to
  stderr and exits with the code.
- Blocking calls are plain synchronous code. The only concurrency is a `ThreadPoolExecutor` for
  parallel ssh status calls in `list`.

### 2.1 Local modules

**`core.py` - errors, paths, settings, records, locks, secrets, subprocess (260 lines)**

```python
class CrabError(Exception):             # .code: 1 error, 2 usage, 3 approval/limit/would lose work, 4 no machine, 5 setup failed
    def __init__(self, message: str, code: int = 1): ...

def home() -> Path                      # ~/.crab-student
def workers_dir() -> Path               # ~/.crab-student/workers
def ssh_dir() -> Path                   # ~/.crab-student/ssh (id_ed25519, config, known_hosts, github_known_hosts)
def vast_key_file() -> Path             # $XDG_CONFIG_HOME/vastai/vast_api_key or ~/.config/vastai/vast_api_key

DEFAULTS: dict[str, object]             # section 3.2
def load_config() -> dict               # defaults overlaid with config.json
def set_config(key: str, value: str | None) -> object   # parse by the default's type, validate, save; None = reset
def parse_value(key: str, text: str) -> object          # "1.5" -> float, "CA, europe" -> ["CA","europe"]; CrabError(2) if invalid

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
def check_name(name: str) -> None       # CrabError(2) with the rule in the message

def record_path(name: str) -> Path
def load(name: str) -> dict             # CrabError(1) "no worker named X (see crab-student list)"
def all_records() -> list[dict]         # sorted by created
def save(rec: dict) -> None             # atomic write (tmp + os.replace)
def remove(name: str) -> None           # record, brief, lock and deploy-key files
@contextmanager
def locked(name: str, wait: float = 0) -> Iterator[None]   # OS lock on workers/<name>.lock; CrabError(1)
                                        # "busy: another crab-student command is working on X" after `wait` s
def is_locked(name: str) -> bool        # try-lock; tells "launching" from "launch interrupted"

def run(cmd: list[str], *, input: str | bytes | None = None, cwd: Path | None = None,
        env: dict | None = None, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess
                                        # text mode unless input is bytes; CrabError with stderr's last line when check fails

def get_secret(key: str) -> str | None  # keyring service "crab-student"; file fallback ~/.crab-student/secrets/<key> (0600)
def set_secret(key: str, value: str) -> None
def delete_secret(key: str) -> None
def secret_names() -> list[str]         # task secrets, from secrets.json (names only; a keyring cannot list)
def ceil_cents(x: float) -> float
def fmt_hours(h: float) -> str          # 1.2 -> "1h12m"
```

Locks use `msvcrt.locking` on Windows and `fcntl.flock` elsewhere. The lock is held for the life of
the command, and an OS lock dies with its process. "Is a launch still running?" is answered by
"can I take its lock?". There are no pid files and no stale-lock breaking.

**`vast.py` - the Vast REST client (100 lines)**

```python
class RentUnknown(Exception): ...       # timeout, connection error or 5xx on the rent request
class VastError(CrabError): ...         # a definite error response (4xx), with Vast's message

def base_url() -> str                   # os.environ.get("VAST_URL", "https://console.vast.ai")
def request(method: str, path: str, body: dict | None = None, *, timeout: float = 30,
            retry: bool = True) -> dict # urllib; Bearer key from core.vast_key_file(); JSON in and out;
                                        # retry=True: 3 tries with 2 s / 5 s backoff on connection errors and 5xx
def search(query: dict) -> list[dict]   # POST /api/v0/bundles/ -> ["offers"]
def rent(offer_id: int, body: dict) -> int   # PUT /api/v0/asks/<id>/, retry=False, timeout 60; -> ["new_contract"];
                                        # raises RentUnknown or VastError; never retried
def instances() -> list[dict]           # GET /api/v1/instances/?limit=25&after_token=... until next_token is null
def instance(instance_id: int) -> dict | None   # GET /api/v0/instances/<id>/ -> ["instances"]; 404 -> None
def set_state(instance_id: int, state: str) -> None   # PUT /api/v0/instances/<id>/ {"state": ...}
def destroy(instance_id: int) -> None   # DELETE; 404 is success
def add_ssh_key(instance_id: int, public_key: str) -> None   # POST /api/v0/instances/<id>/ssh/
def current_user() -> dict              # GET /api/v0/users/current (init: email, credit)
```

**`offers.py` - requirements, search, the picking rule, the budget (170 lines)**

```python
@dataclass
class Req:
    hours: float; gpus: int = 0; gpu_ram_gb: float = 0; gpu_models: list[str] = field(default_factory=list)
    cpus: int = 4; ram_gb: float = 16; disk_gb: float = 50; min_down_mbps: float = 200
    @property
    def gpu(self) -> bool               # gpus > 0

@dataclass
class Plan:
    req: Req; primary: dict; fallbacks: list[dict]; budget_usd: float; auto_budget: bool
    def limit_hours(self, offer: dict) -> float   # budget_usd / offer["dph_total"]

REGIONS: dict[str, set[str]]            # continent -> ISO country codes (section 4.1)
def query(req: Req, cfg: dict) -> dict  # the server-side filter (section 4.1)
def fits(o: dict, req: Req, cfg: dict, now: float) -> bool   # client-side recheck: every filter + regions + models + end_date
def country(o: dict) -> str | None      # "California, US" -> "US"
def rank(offers: list[dict], gpu: bool) -> list[dict]        # the deterministic ordering (section 4.1)
def plan(req: Req, cfg: dict, budget: float | None) -> Plan  # search, fit, rank, primary + fallbacks, budget;
                                        # CrabError(4) no machine fits / CrabError(3) budget too small
def summary(o: dict) -> str             # "1x RTX 4090 24 GB, 16 cores, 64 GB RAM, Quebec, CA - $0.42/hr"
```

**`github.py` - gh and git (150 lines)**

```python
@dataclass
class Repo:                             # the user's local repo
    top: Path; slug: str; head: str

def gh(*args: str, input: str | None = None) -> str   # runs `gh`, returns stdout; CrabError with gh's message
def login() -> str                      # gh api user --jq .login
def git_url(slug: str) -> str           # git@github.com:<slug>.git, or <CRAB_STUDENT_GITHUB_URL>/<slug>.git (tests)
def local_repo(path: Path) -> Repo      # rev-parse --show-toplevel; the github.com remote (origin first); CrabError(2) if none
def check_repo(slug: str, branch: str) -> None   # CrabError(1) no admin rights (deploy key impossible); CrabError(2) branch exists
def snapshot(repo: Repo) -> tuple[str, list[str], list[str]]
                                        # (start_sha, carried tracked files, untracked files NOT carried); section 4.11
def ensure_workspace(login: str, name: str) -> str    # creates <login>/<name> private with auto_init if missing; -> slug
def default_head(slug: str) -> str      # sha of the default branch
def add_deploy_key(slug: str, title: str, pub: str) -> int   # POST repos/<slug>/keys read_only=false -> id
def remove_deploy_key(slug: str, key_id: int) -> None        # 404 is success
def create_branch(rec: dict, key_file: Path) -> None         # user repo: git push <url> <start_sha>:refs/heads/<branch>
                                        # with the deploy key (3 tries, 3 s apart); workspace: POST git/refs from default_head
def branch_sha(slug: str, branch: str) -> str | None
def delete_branch_if_at(slug: str, branch: str, sha: str) -> bool   # only when it still points at sha (no work on it)
def read_report(slug: str, branch: str) -> str | None       # contents API, raw
def pull(rec: dict, path: Path | None) -> str               # fetch agent/<name> without touching the checkout; -> summary
```

**`machine.py` - ssh, upload, terminals (210 lines)**

```python
REMOTE_DIR = "/root/crab-student"
PY = 'exec "$(command -v python3 || echo /venv/main/bin/python)"'   # remote python, resolved on the machine
def alias(name: str) -> str             # "crab-student-<name>"
def address(inst: dict) -> tuple[str, int] | None   # direct ports["22/tcp"][0].HostPort on public_ipaddr, else ssh_host/ssh_port
def write_ssh_config() -> None          # regenerates ~/.crab-student/ssh/config from all records with an address
def set_address(rec: dict, inst: dict) -> bool      # updates rec["ssh"] and the config if it changed; -> changed
def ssh_cmd(name: str, *, tty: bool = False) -> list[str]   # ["ssh", "-F", cfg, (-t), alias]
def remote(name: str, args: list[str], *, input: bytes | None = None, timeout: float = 60) -> dict
                                        # runs `cd /root/crab-student && <PY> cs.py <args>`; parses the last stdout
                                        # line as JSON; CrabError(1) "could not reach NAME: ..." on ssh failure
def reachable(name: str) -> bool        # ssh true with ConnectTimeout=8
def wait_ssh(name: str, deadline: float) -> None
def bundle(rec: dict, extra: dict[str, tuple[bytes, int]]) -> bytes   # tar.gz of remote/* + generated files, with modes
def upload_and_setup(rec: dict, extra: dict[str, tuple[bytes, int]], on_line: Callable[[str], None]) -> None
                                        # one ssh call: untar + cs.py setup; streams "step i/5 ..." lines;
                                        # CrabError(5, "setup failed at step i (label): error") on failure
def open_one(name: str) -> int          # TTY: ssh -t attach here; no TTY on Windows with wt: new wt window; else CrabError(2)
def open_all(names: list[str]) -> int   # Windows: one wt window with two balanced columns; else a local tmux session
def open_vscode(rec: dict) -> int       # code --remote ssh-remote+<alias> <repo_dir>
```

**`launch.py` - launch, bring-up, approval, start, extend (310 lines)**

```python
ONSTART: str                            # the onstart command (section 3.5), a constant
BOOT_TIMEOUT = 15 * 60; POLL = 5; LABEL_WAIT = 120; SSH_WAIT = 180

@dataclass
class LaunchArgs:
    name: str; req: Req; budget: float | None; brief: str; repo_path: Path | None; no_repo: bool
    secrets: list[str]; model: str; fake_claude: bool; dry_run: bool

def launch(a: LaunchArgs, out: Progress) -> dict        # validate, repo plan, plan, gate; pending or bring_up; -> result
def running_count(exclude: str | None = None) -> int    # records whose machine is or may be running (section 4.3)
def gate(budget: float, cfg: dict, running: int) -> str | None   # the reason approval is needed, or None
def bring_up(name: str, out: Progress) -> dict          # drives a record from any launch phase to "ready" (section 4.4)
def rent_loop(rec: dict, out: Progress) -> None         # primary then fallbacks; label recovery; boot wait; destroy non-booted
def wait_boot(rec: dict, out: Progress) -> str | None   # None when running + ssh ok, else the failure reason
def prepare_repo(rec: dict) -> None                     # deploy key + branch (idempotent: skips what the record says is done)
def setup_machine(rec: dict, out: Progress) -> None     # upload_and_setup with generated files; destroys on setup failure
def approve(name: str, out: Progress) -> dict           # performs the pending action (launch/start/extend) without the gate
def decline(name: str) -> str                           # pending launch: remove the record; pending start/extend: clear it
def start(name: str, out: Progress) -> dict             # stopped: gate, Vast start, wait, set-limit; unfinished launch: bring_up
def extend(name: str, hours: float) -> dict             # gate on the new budget; set-limit now or at the next start
def result_line(rec: dict, state: str) -> str           # section 7.3

class Progress:                                         # progress lines go to stderr (so --json stdout stays one value)
    def line(self, text: str) -> None
    def status(self, text: str) -> None                 # rewrites the same line on a TTY; else prints a new line on change
```

**`workers.py` - the views, stop, delete, send, report (240 lines)**

```python
@dataclass
class View:
    name: str; state: str; message: str; used_h: float | None; spent: float | None; budget: float | None
    limit_h: float | None; idle_min: float | None; machine: str; dph: float | None; branch: str; repo: str
    git: dict | None; vast_status: str | None; pending: dict | None; error: str | None

def views(names: list[str] | None = None) -> list[View] # one instances() call + parallel `cs.py status` over ssh;
                                                        # updates rec.last_seen and addresses under each lock
def display_state(rec: dict, inst: dict | None, st: dict | None, locked: bool) -> tuple[str, str]   # section 4.15
def stop(name: str, out: Progress) -> dict              # save over ssh, then Vast stop
def delete(name: str, force: bool, out: Progress) -> dict   # the safety check (section 4.13), destroy, clean up
def send(name: str, text: str) -> None                  # cs.py send (text on stdin)
def report(name: str) -> str                            # REPORT.md from the branch on GitHub
def watch(interval: float = 30) -> None                 # list --watch loop with alerts (section 4.16)
```

**`setup.py` - `crab-student init` and the Claude Code integration (140 lines)**

```python
ALLOW: list[str]; ASK: list[str]        # permission rules (section 6.3)
def init(renew_claude_token: bool = False) -> None      # the steps in section 4.18, each skipped when already done
def install_claude_files() -> None      # ~/.claude/skills/crab-student/SKILL.md; merges ALLOW/ASK into ~/.claude/settings.json
def ensure_ssh_key() -> None            # ssh-keygen -t ed25519 -N "" into ~/.crab-student/ssh/id_ed25519
def ensure_ssh_include() -> None        # "Include ~/.crab-student/ssh/config" as the first line of ~/.ssh/config
def guide(topic: str) -> str            # package data (only "launch")
def check_ready() -> None               # used by launch/offers: Vast key, SSH key, Claude token, gh; CrabError(1) "run crab-student init"
```

**`cli.py` - the parser, handlers and output (330 lines)**

```python
def main(argv: list[str] | None = None) -> int          # parse, dispatch, map CrabError to exit codes, print errors
def build_parser() -> argparse.ArgumentParser
def print_table(views: list[View]) -> None              # section 7.4
def print_status(v: View) -> None                       # section 7.5
def emit(result: dict, text: str, as_json: bool) -> None   # --json: exactly one JSON value on stdout
```

All output is ASCII. `--json` puts exactly one JSON value on stdout, and progress goes to stderr.

The commands (REQ = `--gpus N --gpu-ram GB --gpu-models LIST --cpus N --ram GB --disk GB --min-down MBPS`):

```
crab-student init [--claude-token]
crab-student config [KEY [VALUE]] [--reset] [--json]
crab-student offers [REQ] [--hours H] [--budget USD] [--count N] [--json]
crab-student launch NAME --hours H [REQ] [--budget USD] [--brief-file PATH]   (else the brief on stdin)
                    [--repo PATH | --no-repo] [--secret NAME]... [--model M] [--fake-claude] [--dry-run] [--json]
crab-student approve NAME [--json]          crab-student decline NAME
crab-student list [--watch] [--json]        crab-student status NAME [--json]
crab-student open (NAME | --all) [--vscode] crab-student send NAME [TEXT...]   (else the text on stdin)
crab-student report NAME                    crab-student pull NAME [--repo PATH]
crab-student stop NAME [--json]             crab-student start NAME [--json]
crab-student extend NAME HOURS [--json]     crab-student delete NAME [--force] [--json]
crab-student secret set NAME | secret list [--json] | secret delete NAME
crab-student guide launch
```

`--version` prints the version. A command given without a required argument prints its usage and
exits 2.

### 2.2 Machine side (`remote/`, uploaded flat to `/root/crab-student/`)

**`cs.py` - everything on the machine (470 lines, Python 3.8 standard library)**

```python
ROOT = Path(os.environ.get("CS_ROOT", "/root"))         # tests point this at a temp folder
D = ROOT / "crab-student"; OUTPUTS = ROOT / "outputs"
TICK = float(os.environ.get("CS_TICK_SECONDS", "60"))   # test knob only

def main(argv) -> int                   # dispatch; one-shot commands print one JSON object on the last line
# commands
def cmd_setup() -> int                  # the five steps (section 4.6); prints "step i/5 label" then {"ok": ...}
def cmd_watch() -> int                  # the watchdog loop (single instance by flock on D/watch.lock)
def cmd_claude() -> int                 # runs in the tmux pane: starts/resumes Claude, restarts on Enter
def cmd_attach() -> int                 # exec tmux attach -t worker, or a login shell with a note
def cmd_status() -> int                 # -> status JSON (section 3.6); also restarts a dead watchdog (4.8)
def cmd_save(reason: str) -> int        # -> {"committed": n, "pushed": bool, "skipped": [...], "dirty": n, "unpushed": n, "error": ...}
def cmd_check() -> int                  # save, then -> {"unpushed", "dirty", "outputs", "large_files"} (section 4.13)
def cmd_set_limit(hours: float) -> int  # config.json limit_hours -> {"limit_hours", "used_hours"}
def cmd_send() -> int                   # stdin text -> tmux paste (bracketed) + Enter -> {"sent": true}
def cmd_declare(state: str | None, message: str) -> int  # `crab-status`; checks before "done" (section 4.12)
def cmd_hook(name: str, args: list[str]) -> int          # pre-commit (secrets, big files) and pre-push (own branch only)
# the watchdog
def tick(st: dict, cfg: dict, now: float, probe: Probe) -> list[str]   # pure rule step -> actions:
                                        # "nudge", "warn", "stop:idle", "stop:time-limit", "notify:<state>"
class Probe:                            # real measurements; tests pass a fake
    def transcript_bytes(self) -> int   # total size of ~/.claude/projects/*/*.jsonl
    def cpu_seconds(self) -> float | None   # cgroup v2 cpu.stat usage_usec, else v1 cpuacct.usage
    def gpu_busy(self) -> bool          # nvidia-smi utilization.gpu max >= 10
def ensure_worker_session(cfg: dict, restarted: bool) -> None   # tmux new-session -d -s worker ... "cs.py claude"
def save(reason: str) -> dict           # section 4.10
def stop_machine(st: dict, reason: str) -> None   # notify, save, Vast stop via CONTAINER_API_KEY
def notify(cfg: dict, event: str, text: str) -> None   # ntfy / Slack / Discord / generic JSON (section 4.17)
def type_to_worker(text: str) -> None   # tmux load-buffer + paste-buffer -p + Enter
def git(*args, check=True, timeout=120) -> str
def worker_env(cfg: dict) -> dict       # section 4.7
```

**`fake_claude.py` - a stand-in for `claude` (70 lines)**. It takes the same flags we pass
(`--session-id`, `--resume`, `--model`, `--dangerously-skip-permissions`, a prompt). It appends
JSONL lines to `~/.claude/projects/<slug>/<sid>.jsonl`, so the watchdog sees progress. On a new
session it runs the script from section 8.3: `crab-status working`, write `result.txt`, commit,
push, write `REPORT.md`, commit, push, `crab-status done`. On `--resume` it writes `resumed` to the
transcript. It then echoes stdin lines into the transcript, like a person typing to it.
`FAKE_CLAUDE_SLEEP` (seconds between steps) makes it slow for tests.

**`worker.md`** becomes `~/.claude/CLAUDE.md` with `{{name}}`, `{{branch}}`, `{{repo_dir}}`,
`{{secrets}}` and `{{idle_minutes}}` filled in (section 6.4). It is content, not code.

### 2.3 Line budget and how the design stays within about 2,500

| File | Budget |
|---|---|
| cli.py | 330 |
| core.py | 260 |
| launch.py | 310 |
| workers.py | 240 |
| machine.py | 210 |
| offers.py | 170 |
| github.py | 150 |
| setup.py | 140 |
| vast.py | 100 |
| `__init__`/`__main__` | 10 |
| **local** | **1,920** |
| remote/cs.py | 470 |
| remote/fake_claude.py | 70 |
| **machine** | **540** |
| **total source** | **about 2,460** |

The Markdown content is not counted in that total, but it has limits of its own: SKILL.md 120
lines or fewer, launch.md 130, worker.md 130.

These choices keep the code small:

- There is no service, no API server, no job framework and no async. Each command is straight-line
  code over files and subprocesses.
- The system `ssh` is used instead of an SSH library. It is needed anyway for `open`, VS Code and
  Windows Terminal, and it gives host keys, config and agent handling for free.
- One machine script with subcommands replaces a supervisor, a keeper, a ctl and status scripts.
  The worker is interactive Claude Code in tmux, so there is no stream-json parsing, no message
  queue and no transcript rendering.
- Vast's own `onstart` carries the dead-man switch, so no extra file needs to be uploaded before it
  works.
- `bring_up` handles every launch phase, so interrupted launches, failed launches and approvals all
  go through one path, with no special recovery code.
- Views are computed when asked, from Vast and one ssh call per machine. No state is cached except
  `last_seen`, which is kept for stopped machines.

---

## 3. Data formats

### 3.1 Local files

```
~/.crab-student/
  config.json                  user settings (only keys that differ from the defaults)
  secrets.json                 {"names": ["HF_TOKEN", ...]}  (names of task secrets; values are in the keyring)
  secrets/<key>                0600, only when no keyring backend works (keys: claude-token, secret:<NAME>)
  workers/<name>.json          the record (3.3)
  workers/<name>.lock          the record's lock file (empty)
  workers/<name>.brief.md      the task brief as given
  workers/<name>.key(.pub)     the worker's deploy key (ssh-keygen ed25519, no passphrase), kept until delete
  ssh/id_ed25519(.pub)         crab-student's key for machines (made by init with ssh-keygen)
  ssh/config                   generated Host entries (3.8)
  ssh/known_hosts              machines' host keys, stored under HostKeyAlias crab-student-<name>
  ssh/github_known_hosts       GitHub's pinned ed25519 host key (written by init)
~/.config/vastai/vast_api_key  the Vast key (shared with the vastai CLI; honours XDG_CONFIG_HOME), 0600
~/.ssh/config                  gets one line at the top: Include ~/.crab-student/ssh/config
~/.claude/skills/crab-student/SKILL.md, ~/.claude/settings.json (permission rules merged in)
```

Keys are always created with `ssh-keygen`, never written by Python. On Windows this gives the key
files the ACLs that OpenSSH insists on.

### 3.2 Settings (`crab-student config`) and defaults

| Key | Default | Type, check | Meaning |
|---|---|---|---|
| `auto_approve_usd` | 50.0 | float >= 0 | Budgets above this wait for `approve` |
| `max_running_machines` | 5 | int >= 1 | Starting one more than this waits for `approve` |
| `budget_factor` | 2.0 | float >= 1 | Budget = factor x estimated cost |
| `min_budget_hours` | 2.0 | float > 0 | Budget always covers at least this many machine hours |
| `idle_minutes` | 60 | int >= 1 | Idle stop after this long without activity |
| `image` | `vastai/base-image:cuda-12.8.1-auto` | str | Docker image |
| `disk_gb` | 50 | int >= 10 | Disk when the launch does not say |
| `min_reliability` | 0.98 | 0..1 | Host reliability floor (`reliability2`) |
| `min_download_mbps` | 200 | float >= 0 | Download-speed floor when the launch does not say |
| `regions` | `[]` | list of ISO country codes or continent names (section 4.1); empty = anywhere | Where machines may be |
| `model` | `opus` | str | Claude model for workers (`--model`) |
| `workspace_repo` | `crab-student-work` | repo name | Repo for `--no-repo` tasks, under the user's login |
| `notify_url` | `""` | "" or http(s) URL | Notifications (section 4.17) |

- `crab-student config` prints every key with its value and marks the ones left at the default.
- `config KEY` prints one value.
- `config KEY VALUE` sets it. Lists are comma-separated, for example `config regions "CA, US, europe"`.
- `config KEY --reset` returns a key to its default.
- Unknown keys and invalid values exit 2 with the rule in the message.

### 3.3 The worker record (`workers/<name>.json`)

```json
{
  "name": "ablation-1",
  "created": "2026-09-30T14:02:11Z",
  "phase": "ready",
  "error": null,
  "req": {"hours": 3.0, "gpus": 1, "gpu_ram_gb": 24, "gpu_models": [], "cpus": 8, "ram_gb": 32,
          "disk_gb": 60, "min_down_mbps": 200},
  "budget_usd": 2.52,
  "auto_budget": true,
  "limit_hours": 6.0,
  "limit_pushed": true,
  "model": "opus",
  "fake_claude": false,
  "secrets": ["HF_TOKEN"],
  "session_id": "6f1c2c3e-...-uuid4",
  "repo": {"slug": "me/proj", "workspace": false, "branch": "agent/ablation-1",
           "start_sha": "1a2b3c4...", "carried": ["train.py", "configs/a.yaml"], "untracked": ["notes.txt"],
           "deploy_key_id": 98765, "branch_created": true, "dir": "/root/work/proj"},
  "offers": [{"id": 123456, "machine_id": 777, "dph_total": 0.42, "summary": "1x RTX 4090 24 GB, ..."},
             {"id": 123999, "machine_id": 912, "dph_total": 0.44, "summary": "..."}],
  "failed_offers": [{"id": 123000, "reason": "offer no longer available"}],
  "rent": {"label": "cs:ablation-1:3fa2b9c1", "offer_id": 123456},
  "instance_id": 987654,
  "dph": 0.42,
  "machine": "1x RTX 4090 24 GB, 16 cores, 64 GB RAM, Quebec, CA",
  "gpu_host_for_cpu": false,
  "ssh": {"host": "203.0.113.7", "port": 40122},
  "uploaded": true,
  "pending": null,
  "last_seen": {"at": "2026-09-30T16:10:00Z", "state": "working", "message": "epoch 3/10",
                "used_hours": 1.2, "stop_reason": null, "git": {"dirty": 0, "unpushed": 0}}
}
```

`phase` is how far the launch got. Only the local side writes it:

| Phase | Meaning | Next |
|---|---|---|
| `pending` | Waiting for approval; nothing rented, nothing on GitHub | `approve` -> `renting`; `decline` -> removed |
| `renting` | `rent.label` written, rent request sent or about to be sent | `booting` (instance id known) or stays (outcome unknown) |
| `booting` | `instance_id` known, waiting for running + ssh | `setup`, or back to `renting` with the next offer after destroying it |
| `setup` | `uploaded` set to true just before the upload starts | `ready`, or `failed` (machine destroyed) |
| `ready` | Launch finished. Whether the machine is running now comes from Vast | - |
| `failed` | Launch failed; `error` says why; `instance_id` is null when the machine was destroyed | `start` retries the launch |

`pending` is `{"action": "launch" | "start" | "extend", "hours": 2.0, "reason": "budget $84.00 is
over auto_approve_usd ($50.00)"}` or null. `last_seen` is a display cache written whenever a command
sees the machine. Decisions never rely on it (delete re-checks the machine).

### 3.4 Secrets locally

- The Claude token is stored in the keyring (service `crab-student`) under key `claude-token`. Init
  checks that it starts with `sk-ant-oat01-` and is at least 80 characters long.
- Task secrets are stored under key `secret:<NAME>`, with the names listed in `secrets.json`.
  `NAME` must match `^[A-Z][A-Z0-9_]{0,63}$`, because it becomes an environment variable on the
  machine. `secret set NAME` always prompts with `getpass`, and exits 2 without a terminal.
- Fallback: when `keyring` raises `NoKeyringError` or its backend is `fail`/`null`, secrets go to
  `~/.crab-student/secrets/<key>` with mode 0600. A one-line warning is printed once, at `init`.

### 3.5 Vast: rent body, label, onstart

The label is `cs:<name>:<8 random hex>`, new for every rent attempt and written to the record
before the request is sent. The rent body is:

```json
{"client_id": "me", "image": "<cfg.image>", "disk": 60, "label": "cs:ablation-1:3fa2b9c1",
 "onstart": "<ONSTART>", "runtype": "ssh_direc ssh_proxy", "cancel_unavail": true,
 "env": {"CS_VAST_URL": "<vast.base_url()>", "NVIDIA_DISABLE_REQUIRE": "1"}}
```

`NVIDIA_DISABLE_REQUIRE` is included only for CPU tasks, where it lets the image start on any GPU
host. `CS_VAST_URL` tells the machine where to send its stop call, which lets the e2e point it at
the fake. The whole of `ONSTART` is below. Every line is a complete command, so if Vast needs a
single line, joining the lines with `; ` works:

```bash
D=/root/crab-student; mkdir -p $D/logs; cd $D; umask 077
env | grep -E '^(CONTAINER_ID|CONTAINER_API_KEY|CS_VAST_URL)=' | sed 's/^/export /' > vast.env
date +%s > booted
nohup bash -c 'cd /root/crab-student; while sleep 60; do t=$(cat booted); h=$(stat -c %Y heartbeat 2>/dev/null || echo 0); [ "$h" -gt "$t" ] && t=$h; if [ $(( $(date +%s) - t )) -gt 2700 ]; then . ./vast.env; curl -s -m 30 -X PUT -H "Authorization: Bearer $CONTAINER_API_KEY" -H "Content-Type: application/json" -d "{\"state\":\"stopped\"}" "${CS_VAST_URL:-https://console.vast.ai}/api/v0/instances/$CONTAINER_ID/"; fi; done' > logs/deadman.log 2>&1 &
[ -f setup-done ] && nohup "$(command -v python3 || echo /venv/main/bin/python)" cs.py watch >> logs/watch.log 2>&1 &
```

### 3.6 Files on the machine

`D = /root/crab-student`. The repo is at `/root/work/<repo name>` (for `--no-repo`:
`/root/work/crab-student-work`). Big outputs go in `/root/outputs/`.

| Path | Mode | Written by | Content |
|---|---|---|---|
| `D/cs.py`, `D/fake_claude.py`, `D/worker.md` | 0755/0755/0644 | upload | The machine code and the worker's rules template |
| `D/config.json` | 0600 | upload, `set-limit` | Below. 0600 because `notify_url` may carry a webhook secret |
| `D/BRIEF.md` | 0644 | upload | The task brief |
| `D/claude.env` | 0600 | upload | `export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-...'` |
| `D/secrets.env` | 0600 | upload | `export HF_TOKEN='...'` one line per attached secret (values shell-quoted) |
| `D/deploy_key` | 0600 | upload | The worker's deploy key (private) |
| `D/github_known_hosts` | 0644 | upload | GitHub's pinned host key |
| `D/vast.env` | 0600 | onstart | `export CONTAINER_ID=...`, `CONTAINER_API_KEY`, `CS_VAST_URL` |
| `D/booted` | 0600 | onstart | Epoch seconds of this container start |
| `D/heartbeat` | 0600 | watchdog | Touched every tick (the dead-man switch reads its mtime) |
| `D/setup-done` | 0600 | setup step 5 | Empty; its presence makes onstart start the watchdog |
| `D/state.json` | 0600 | watchdog | Below |
| `D/worker.json` | 0644 | `crab-status` | `{"state": "working", "message": "epoch 3/10", "at": 1727700000}` |
| `D/hooks/pre-commit`, `D/hooks/pre-push` | 0755 | setup | `#!/bin/sh` + `exec <python> /root/crab-student/cs.py hook <name> "$@"` |
| `D/logs/{setup,watch,deadman}.log` | 0600 | each process | Plain logs. Never environment dumps or secrets |
| `D/session_id` | 0600 | `cs.py claude` | Only when a saved conversation could not be resumed: the replacement session id (overrides `config.json`) |
| `/usr/local/bin/crab-status` | 0755 | setup | `#!/bin/sh` + `exec <python> /root/crab-student/cs.py declare "$@"` |
| `/usr/local/bin/crab-attach` | 0755 | setup | `#!/bin/sh` + `exec <python> /root/crab-student/cs.py attach` (the remote command of `open`) |
| `/etc/profile.d/crab-student.sh` | 0644 | setup | `export PATH=/root/.local/bin:/venv/main/bin:$PATH` (login shells, VS Code) |
| `/root/.no_auto_tmux` | 0644 | setup | Stops Vast's automatic tmux on ssh logins |
| `/root/.tmux.conf` | 0644 | setup | `set -g mouse on`, `set -g history-limit 50000`, `set -g status-left "[<name>] "`, `set -g status-right "detach: Ctrl-b d"` |
| `/root/.claude/CLAUDE.md` | 0644 | setup | `worker.md` filled in |
| `/root/.claude/settings.json` | 0644 | setup | Section 6.5 |
| `/root/.claude.json` | 0600 | setup (merge) | `hasCompletedOnboarding`, `bypassPermissionsModeAccepted`, `projects[<repo dir>]` trust flags |

`config.json`:

```json
{"name": "ablation-1", "branch": "agent/ablation-1", "repo_url": "git@github.com:me/proj.git",
 "repo_dir": "/root/work/proj", "model": "opus", "fake_claude": false, "session_id": "6f1c...",
 "idle_minutes": 60, "limit_hours": 6.0, "notify_url": "", "secret_names": ["HF_TOKEN"]}
```

`state.json` (owned by the watchdog; kept across restarts because the disk survives):

```json
{"used_seconds": 4380, "last_activity": 1727700000, "last_tick": 1727700060, "boot": 1727690000,
 "transcript_bytes": 123456, "cpu_seconds": 812.4, "stopping": null, "stop_reason": null,
 "notified_state": "working", "nudged_at": null, "warned_limit": false}
```

`cs.py status` output, which is what `list` and `status` consume:

```json
{"name": "ablation-1", "worker": {"state": "working", "message": "epoch 3/10", "at": 1727700000},
 "claude_running": true, "idle_seconds": 120, "used_hours": 1.22, "limit_hours": 6.0,
 "stopping": null, "stop_reason": null, "setup_done": true,
 "git": {"branch": "agent/ablation-1", "head": "9f8e7d6", "dirty": 0, "unpushed": 0}}
```

`dirty` counts uncommitted changes to tracked files plus untracked files that are not ignored.
`unpushed` is the number of commits ahead of the upstream in `git status --porcelain=v2 --branch`.
It needs no network, and it is exact after the worker's own pushes.

### 3.7 The ssh commands

Each one-shot command runs as `ssh -F <cfg> crab-student-<name> 'cd /root/crab-student && exec
"$(command -v python3 || echo /venv/main/bin/python)" cs.py <args>'` and prints one JSON object on
its last stdout line. When a command fails, it prints `{"error": "<message>"}` and exits 1.

| Command | Stdin | Output | Used by |
|---|---|---|---|
| `tar -xzf - -C D && ... cs.py setup` | tar.gz | lines `step i/5 <label>`, then `{"ok": true}` or `{"ok": false, "step": i, "error": "..."}` | launch |
| `status` | - | status JSON (3.6) | list, status |
| `save --reason user` | - | `{"committed": 3, "pushed": true, "skipped": ["data/big.bin"], "dirty": 1, "unpushed": 0, "error": null}` | stop |
| `check` | - | save's fields plus `"outputs": {"files": 12, "bytes": 5.1e9, "sample": [...]}, "large_files": [{"path": "ckpt/last.pt", "bytes": 2.1e9}]` | delete |
| `set-limit 8.0` | - | `{"limit_hours": 8.0, "used_hours": 5.1}` | start, extend |
| `send` | the text | `{"sent": true}` | send |
| `crab-attach` (a plain remote command, with `ssh -t`) | the terminal | an interactive tmux attach | open |

### 3.8 The ssh config (`~/.crab-student/ssh/config`, regenerated from the records)

```
# Generated by crab-student. Do not edit: rewritten on every launch, start and list.
Host crab-student-ablation-1
  HostName 203.0.113.7
  Port 40122
  User root
  IdentityFile "C:/Users/me/.crab-student/ssh/id_ed25519"
  IdentitiesOnly yes
  UserKnownHostsFile "C:/Users/me/.crab-student/ssh/known_hosts"
  HostKeyAlias crab-student-ablation-1
  StrictHostKeyChecking accept-new
  ServerAliveInterval 30
  ServerAliveCountMax 4
  BatchMode yes
```

`BatchMode yes` applies to our own calls. Interactive `open` passes `-o BatchMode=no`, which does
no harm because keys are the only way to log in. Paths are written with forward slashes and quoted.
`delete` removes the worker's lines from `known_hosts` with `ssh-keygen -R crab-student-<name> -f ...`.

### 3.9 Names

| Thing | Form |
|---|---|
| Worker name | `^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$` (3-40 characters) |
| Branch | `agent/<name>` |
| SSH alias | `crab-student-<name>` |
| Vast label | `cs:<name>:<8 hex>` |
| Deploy key title | `crab-student <name>` |
| tmux session | `worker` |
| Workspace repo | `<login>/<workspace_repo>` (private, created with `auto_init`) |
| Autosave commit | `crab-student: autosave (<reason>)` |
| Snapshot commit | `crab-student: uncommitted changes at launch` |

---

## 4. Key algorithms and rules

### 4.1 Offer search and the picking rule

**Requirements** come from the launch flags (`--hours` is required; the rest have defaults):
`--gpus N` (default 0, which means a CPU task), `--gpu-ram GB` per GPU, `--gpu-models "4090,A100"`,
`--cpus N` (4), `--ram GB` (16), `--disk GB` (`disk_gb`), `--min-down MBPS` (`min_download_mbps`).

**Server query** (`offers.query`). Vast counts 1 GB as 1000 MB:

```python
q = {"rentable": {"eq": True}, "rented": {"eq": False}, "verified": {"eq": True}, "external": {"eq": False},
     "reliability2": {"gte": cfg["min_reliability"]}, "cpu_cores_effective": {"gte": req.cpus},
     "cpu_ram": {"gte": req.ram_gb * 1000}, "disk_space": {"gte": req.disk_gb},
     "inet_down": {"gte": req.min_down_mbps},
     "type": "on-demand", "order": [["dph_total", "asc"]], "limit": 1000, "allocated_storage": req.disk_gb}
if req.gpu:
    q["num_gpus"] = {"gte": req.gpus}
    q["gpu_ram"] = {"gte": req.gpu_ram_gb * 1000}
    if cuda := image_cuda(cfg["image"]):          # "cuda-12.8.1" in the tag -> 12.8; no filter if the tag has none
        q["cuda_max_good"] = {"gte": cuda}
```

**Client recheck** (`offers.fits`). The server filter is not always exact, so every condition above
is checked again, and these are added:

- `verification == "verified"` when the field is present.
- `reliability2`, or `reliability` when that is missing, is at least the floor.
- `dph_total > 0`.
- If `end_date` is present: `end_date >= now + 3600 * (budget_factor * hours + 1)`.
- GPU models: each entry of `--gpu-models`, compared case-insensitively, must be a substring of
  `gpu_name` (`"4090"` matches `"RTX 4090"`). This filter is only applied client-side, because Vast
  needs exact names.
- Regions: when `regions` is not empty, `country(o)` must be in the union of the listed ISO codes and
  continents. Offers with no country are excluded. `country` is the text after the last comma of
  `geolocation`, stripped and upper-cased, and it must be 2 letters.

```python
REGIONS = {  # continent -> countries; entries of `regions` are these names or 2-letter ISO codes
  "north-america": "US CA MX", "south-america": "AR BR CL CO PE UY",
  "europe": "AT BE BG CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LT LU LV MT NL NO PL PT RO RS SE SI SK UA",
  "asia": "AE HK ID IL IN JP KR MY PH SG TH TR TW VN", "oceania": "AU NZ", "africa": "EG KE MA NG ZA"}
```

For CPU tasks the search has no GPU conditions at all. Cheap GPU hosts compete on price with
CPU-only hosts. When a CPU task rents a GPU host, `NVIDIA_DISABLE_REQUIRE=1` is added to the rent
env and the record's `gpu_host_for_cpu` is set to true.

**Ranking** (`offers.rank`) is deterministic:

```python
def rank(offers, gpu):
    pool = offers
    if gpu:                                          # price for speed, when the difference is clear
        cheapest = min(o["dph_total"] for o in offers)
        band = [o for o in offers if o["dph_total"] <= 1.5 * cheapest]
        best = max(value(o) for o in band)           # value = dlperf / dph_total (0 when dlperf is missing)
        if best > 0:
            pool = [o for o in band if value(o) >= best / 1.2]
    floor = min(o["dph_total"] for o in pool)
    similar = sorted((o for o in pool if o["dph_total"] <= 1.10 * floor),
                     key=lambda o: (-o.get("inet_down", 0), o["dph_total"], o["id"]))   # similar price: faster download
    rest = sorted((o for o in offers if o not in similar),
                  key=lambda o: (o["dph_total"], -o.get("inet_down", 0), o["id"]))
    return similar + rest
```

In words:

- **CPU tasks** take the cheapest offer.
- **GPU tasks** first drop offers whose price-for-speed is more than 20% worse than the best offer
  priced up to 1.5 times the cheapest. A faster GPU therefore wins only when it clearly pays for itself.
- **Download speed:** among offers within 10% of the lowest price left, the one with the fastest
  download wins.
- **Ties** are broken by price, then by offer id.

**Primary and fallbacks:** the primary is `ranked[0]`. There are up to 2 fallbacks, the next offers
in ranked order that meet both of these conditions:

- a `machine_id` different from the primary and from each other (a boot failure is often the host's fault);
- `dph_total <= 1.25 x primary`.

`crab-student offers` prints the top `--count` (default 10) of the ranked list, marks the pick and the
fallbacks, and prints the budget that a launch with `--hours` would get. It is read-only.

### 4.2 Budget and time limit

- **Without `--budget`** (the normal case):
  `budget = ceil_cents(primary.dph x max(budget_factor x hours, min_budget_hours))`, and `auto_budget`
  is set to true.
- **With `--budget USD`:** offers are first restricted to those with `dph x hours <= budget`. If none
  is left while offers without that restriction exist, the launch exits 3 with:
  `budget $B is too small: the cheapest fitting machine costs $X/hr, so 3.0h needs at least $Y`.
  If no offer fits the hardware at all, it exits 4.
- **The time limit** is `limit_hours = budget / dph` of the offer actually rented. When a fallback
  is used, the budget stays the same and the limit is recomputed from the fallback's price.
- **Money spent** is shown as `used_hours x dph`. `used_hours` is counted by the watchdog across all
  starts, in `state.json`. It is approximate: it leaves out boot time and storage billed while a
  machine is stopped, which `status` says in one line.
- **`extend NAME HOURS`** adds `HOURS x dph` to the budget and `HOURS` to the limit, and goes through
  the gate (4.3). If the machine is running, the new limit is pushed at once (`cs.py set-limit`).
  If it is stopped, the record gets `limit_pushed: false` and the next `start` pushes it.
- **`start` refuses** (exit 3) when the last known `used_hours` is at or above `limit_hours`:
  `time limit reached (6h00m used of 6h00m). Extend it first: crab-student extend NAME 2`.

The watchdog enforces the limit (4.8). It does not enforce it during the first 5 minutes after a boot
(the *boot grace*), which gives `start` time to push a limit raised while the machine was stopped.

### 4.3 The approval gate

```python
def gate(budget, cfg, running):
    if budget > cfg["auto_approve_usd"]:
        return f"budget ${budget:.2f} is over auto_approve_usd (${cfg['auto_approve_usd']:.2f})"
    if running + 1 > cfg["max_running_machines"]:
        return f"{running} machines are running (max_running_machines is {cfg['max_running_machines']})"
    return None
```

`running_count` makes one `vast.instances()` call and counts the other records that are in
`renting`, `booting` or `setup`, or that are `ready` with Vast `intended_status == "running"` or
`actual_status` in (`running`, `loading`, `created`). The gate applies in three places:

| Action | Budget checked | Machines checked | When a limit is hit |
|---|---|---|---|
| `launch` (and a retried failed launch) | the plan's budget | yes | record `phase: pending`, `pending: {action: launch}` (retry: `{action: start}`), exit 3 |
| `start` of a stopped worker | - (its budget was accepted at launch) | yes | `pending: {action: start}`, exit 3 |
| `extend` | the new total budget | - | `pending: {action: extend, hours}`, exit 3 |

- **`approve NAME`** runs the pending action without the gate. For a launch it:
  1. checks again that the branch is still absent;
  2. plans again with fresh offers, with the approved budget acting like an explicit `--budget`
     (4.2), so the approved amount is a cap;
  3. stores the new `offers`, sets `phase: renting`, `rent: null` and `pending: null`;
  4. calls `bring_up`.

  For a start or an extend, it runs `start` or `extend` with the gate skipped.
- **`decline NAME`** removes a pending launch's record, or clears a pending start or extend.
- **Permissions:** `approve` and `decline` are "ask" commands for Claude Code (6.3). `launch`, `start`
  and `extend` are allowed, because they check the gate themselves.

A pending launch creates nothing on Vast or GitHub. Its snapshot commit (4.11) is made at launch
time, so approving later starts from what the user had when they asked.

### 4.4 Launch, bring-up and the rent loop

`launch(a)` does the checks and planning. Nothing in these steps writes to Vast or GitHub:

1. `setup.check_ready()`, `core.check_name(name)`. A record with that name must not exist (exit 2).
2. The brief comes from `--brief-file` or stdin, and must not be empty (exit 2). Each `--secret NAME`
   must be stored (exit 2: `no secret named HF_TOKEN: run crab-student secret set HF_TOKEN in a
   terminal`).
3. The repo:
   - Default: `local_repo`, `check_repo`, and `snapshot`. The snapshot writes only an unreferenced
     local commit object; `--dry-run` skips `commit-tree` and only lists the changes.
   - `--no-repo`: `slug = <login>/<workspace_repo>`. If the repo exists, the branch must not exist
     on it. The repo itself is created later, in `prepare_repo`, and `start_sha` is set there too
     (the default branch's head).
4. `offers.plan(req, cfg, budget)`.
5. With `--dry-run`, print the plan, the repo line and whether approval would be needed, then return.
6. Create the record (3.3) with a new `session_id` (uuid4), and write the brief file.
7. `gate(...)`: if there is a reason, set `phase: pending`, print the approval result line, exit 3.
8. `bring_up(name)`.

Secret values are read from the keyring only when the upload is built. A secret deleted in the
meantime fails the launch before the upload (exit 1).

`bring_up(name)` is the single path from any launch phase to `ready`. `launch`, `approve` and
`start` all use it, including `start` on an interrupted or failed launch.

```python
def bring_up(name, out):
    with core.locked(name, wait=900):        # a second caller waits for a running launch, then reports its outcome
        rec = core.load(name)
        if rec["phase"] == "ready":   return finish(rec)
        if rec["phase"] == "pending": raise CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
        if rec["phase"] == "failed":  replan(rec)        # fresh offers within rec's budget; phase -> renting, rent -> None
        if rec["phase"] == "renting": rent_loop(rec, out) # -> booting + booted, or raises
        elif rec["phase"] == "booting":
            if (why := wait_boot(rec, out)): discard(rec, why); rent_loop(rec, out)
        prepare_repo(rec)                                 # idempotent; normally already done during the boot wait
        setup_machine(rec, out)                           # phase setup -> ready; raises CrabError(5) after destroying
        return finish(rec)                                # phase ready, ssh config, result dict
```

The rent loop never sends a rent request twice for an unknown outcome:

```python
def rent_loop(rec, out):
    for offer in [o for o in rec["offers"] if o["id"] not in failed_ids(rec)]:
        if rec["rent"] and rec["rent"]["offer_id"] == offer["id"]:   # sent earlier, outcome unknown (interrupted run)
            iid = find_by_label(rec["rent"]["label"])
            if iid is None: raise CrabError(RENT_UNKNOWN.format(...), 1)
        else:
            rec["rent"] = {"label": f"cs:{rec['name']}:{secrets.token_hex(4)}", "offer_id": offer["id"], "at": now()}
            rec["phase"] = "renting"; core.save(rec)                  # written BEFORE the request
            try:
                iid = vast.rent(offer["id"], rent_body(rec, offer))
            except vast.VastError as e:                               # definite refusal (taken, bad request)
                fail_offer(rec, offer, str(e)); continue
            except vast.RentUnknown:
                iid = find_by_label(rec["rent"]["label"])             # polls instances() every 10 s for 120 s
                if iid is None: core.save(rec); raise CrabError(RENT_UNKNOWN.format(...), 1)
        adopt(rec, iid, offer)       # instance_id, dph, machine, limit_hours = budget/dph, phase booting; save
        prepare_repo(rec)            # first time only; on error: destroy (holds nothing), phase failed, raise
        why = wait_boot(rec, out)
        if why is None: return
        discard(rec, why)            # vast.destroy (never received files), failed_offers += offer, instance_id None
    rec["phase"], rec["error"] = "failed", "no offer could be rented or booted"; core.save(rec)
    raise CrabError("all offers failed (...). Try again later: crab-student start NAME, or loosen the requirements.", 4)
```

`RENT_UNKNOWN`:
`Vast did not confirm the rent of offer 123456 and no machine labelled cs:NAME:3fa2b9c1 has appeared.
crab-student will not rent again by itself, so you do not pay twice. Run "crab-student start NAME" in a
few minutes to look again, or "crab-student delete NAME" to give up (then destroy any machine with that
label at https://cloud.vast.ai/instances/).`

**Boot wait** (`wait_boot`). The deadline is `rent.at + 15 min`, and it is kept across interrupted
runs. Every 5 s the loop calls `vast.instance(id)` and prints `actual_status` and `status_msg` when
they change. It ends in one of these ways:

- The instance is gone: failure `"the instance disappeared"`.
- `actual_status` is `exited` or `offline`: failure `"status <s>: <status_msg>"`.
- `status_msg` matches `(?i)\b(error|failed)\b` on two polls in a row: failure with that message
  (this covers "failed to set up container networking").
- `actual_status == "running"` and it has an address: the loop records the address, adds the SSH key
  (`vast.add_ssh_key`, ignoring "already present" errors), and returns success once
  `machine.reachable()` works.
- The deadline passes: failure `"not running after 15 minutes (last status: ...)"`.

`start` uses the same wait for a stopped machine but **never destroys**. On failure it exits 1:
`NAME did not come back up (Vast status: scheduling - the host may be busy). Vast keeps trying;
check again with crab-student status NAME, or stop it with crab-student stop NAME.`

**Destroying is allowed only while `uploaded` is false** (before the upload) or right after a setup
failure (4.6). Nothing else destroys a machine except `delete`.

**Interruptions** (Ctrl-C, a closed terminal, Claude Code's 10-minute Bash cut-off) leave the record
in its phase. The lock dies with the process, so `list` shows `launch interrupted` and suggests
`crab-student start NAME`, which continues through `bring_up`:

| Phase | What `start` does |
|---|---|
| `renting` | Looks for the label |
| `booting` | Waits for the boot again |
| `setup` | Uploads again and reruns setup, which is idempotent |

The launch guide tells the launcher to do exactly this when its command is cut off.

### 4.5 The upload

`machine.bundle` builds a tar.gz in memory from:

- every regular file in the package's `remote/` folder (no subfolders, no dotfiles);
- the generated files: `config.json` (0600), `BRIEF.md` (0644), `claude.env` (0600),
  `secrets.env` (0600, values quoted with `shlex.quote`), `deploy_key` (0600, read from the local key
  file) and `github_known_hosts` (0644).

Modes are set on each `TarInfo`, so secrets are never world-readable, not even for an instant.

One ssh call sends the whole bundle on stdin:

```
ssh -F cfg crab-student-NAME 'mkdir -p /root/crab-student && tar -xzf - -C /root/crab-student --no-same-owner && cd /root/crab-student && exec "$(command -v python3 || echo /venv/main/bin/python)" cs.py setup'
```

Secrets travel only inside the ssh stream and land in 0600 files. They never appear on a command
line or in a log. The local side prints each `step i/5 ...` line as progress. The overall timeout is
10 minutes. `record.uploaded = true` is saved **before** the call starts, because a partial upload
might already hold secrets and a checkout.

### 4.6 Machine setup (`cs.py setup`, idempotent, about 30-45 s)

1. **`environment`**:
   - Write `/etc/profile.d/crab-student.sh`, `/root/.no_auto_tmux`, `/root/.tmux.conf`,
     `/usr/local/bin/crab-status`, `/usr/local/bin/crab-attach`, `D/hooks/*` and `mkdir -p /root/outputs`.
   - `git config --global user.name "crab-student worker (<name>)"` and
     `user.email "<name>@crab-student.invalid"`.
   - Start two background jobs: the Claude Code install
     (`curl -fsSL https://claude.ai/install.sh | bash`, retried once after 5 s; skipped with `--fake-claude`),
     and `apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux` when tmux
     is missing.
2. **`repository`**: if `repo_dir` has no `.git`, clone with the deploy key:
   `GIT_SSH_COMMAND="ssh -i D/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=D/github_known_hosts -o StrictHostKeyChecking=yes"`
   and `git clone --filter=blob:none --branch <branch> <repo_url> <repo_dir>`. Then, in the repo:
   - `core.sshCommand` set to the same ssh command, so the worker's plain `git push`/`git fetch` work;
   - `core.hooksPath=D/hooks`;
   - `remote.origin.push=refs/heads/<branch>:refs/heads/<branch>`.
3. **`worker settings`**:
   - write `/root/.claude/CLAUDE.md` from `worker.md` with the placeholders filled in;
   - write `/root/.claude/settings.json` (6.5);
   - merge into `/root/.claude.json`: `hasCompletedOnboarding: true`,
     `bypassPermissionsModeAccepted: true`, and
     `projects[repo_dir] = {hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true}`,
     keeping any other keys.
4. **`Claude Code`**: wait up to 180 s for the background jobs. Check that `/root/.local/bin/claude
   --version` works (not with the fake) and that `tmux -V` works. On failure, report the last line of
   the install output.

   Then check the token, which catches an expired or wrong token before the worker is handed the task
   (costs about 5 s):

   ```
   claude -p "Reply with the word ok" --model haiku
   ```

   This runs with `worker_env` and a 60 s timeout. A failure is reported as `the Claude token was
   refused (<last output line>): run crab-student init --claude-token, then crab-student start NAME`.
   It is skipped with the fake.
5. **`starting the worker`**:
   - touch `D/setup-done`;
   - start the watchdog detached (`setsid`, `nohup`, output to `logs/watch.log`);
   - wait up to 90 s until tmux session `worker` exists and the conversation file
     `~/.claude/projects/*/<session_id>.jsonl` exists.

   If the file does not appear, the step fails. Its error includes the last 15 lines of
   `tmux capture-pane -p -t worker`, so a new first-run prompt in Claude Code is visible to the
   user instead of a mystery.

On any failure, setup prints `{"ok": false, "step": i, "error": ...}` and exits 1. The local side
then does four things:

- destroys the machine (a failed setup holds no work, because the worker never ran);
- sets `phase: failed` with the error;
- keeps the deploy key and branch, so `start` can retry;
- exits 5 with: `setup failed at step 4 (Claude Code): <error>. The machine was destroyed. Retry: crab-student start NAME`.

### 4.7 Claude Code in tmux: start and resume

The watchdog calls `ensure_worker_session()` at every tick. If `tmux has-session -t worker` fails,
it runs:

```
tmux new-session -d -s worker -x 220 -y 50 -c <repo_dir> '<python> /root/crab-student/cs.py claude'
```

The watchdog removed `CONTAINER_API_KEY` and `CONTAINER_ID` from its own environment when it
started, so the tmux server and everything under it never see them.

`cs.py claude` runs inside the pane:

```python
def cmd_claude():
    first = True
    while True:
        sid = session_id()                       # D/session_id if present (a replaced one), else config["session_id"]
        saved = bool(glob(ROOT / ".claude/projects" / "*" / f"{sid}.jsonl"))
        argv = claude_argv(cfg) + ["--dangerously-skip-permissions", "--model", cfg["model"]]
        # claude_argv: ["/root/.local/bin/claude"], or [python, D/fake_claude.py] with fake_claude
        argv += ["--resume", sid] + ([RESTART_NOTE] if first else []) if saved else ["--session-id", sid, FIRST_PROMPT]
        t0 = time.time()
        rc = subprocess.call(argv, cwd=cfg["repo_dir"], env=worker_env(cfg))
        if saved and rc != 0 and time.time() - t0 < 20:     # the saved conversation could not be resumed
            write D/session_id = uuid4(); FIRST_PROMPT becomes LOST_PROMPT; continue
        first = False
        print("\nClaude Code exited. Press Enter to start it again (same conversation).")
        wait_for_enter()                         # on EOF: sleep 60, then loop
```

- `FIRST_PROMPT`: `You are a crab-student worker. Your task brief is in /root/crab-student/BRIEF.md
  and your rules are in ~/.claude/CLAUDE.md. Read both, run crab-status working "<first step>", then
  start.`
- `RESTART_NOTE`: `crab-student: Claude Code was restarted (the machine was started again, the host
  rebooted, or Claude Code crashed). Background jobs from before are gone. If you were in the middle
  of the task, check where things stand (git log, your logs, checkpoints) and continue. If you had
  finished, reply "ok" and wait.`
- `LOST_PROMPT`: `FIRST_PROMPT` plus `Your previous conversation could not be restored: read
  REPORT.md and git log to see how far you got, then continue.`

`worker_env(cfg)` starts from `os.environ` and changes it like this:

- removes `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT`, so Claude does not think it is nested, and
  removes `CONTAINER_API_KEY` and `CONTAINER_ID`;
- prepends `/root/.local/bin:/venv/main/bin` to `PATH`;
- sets `IS_SANDBOX=1`, `DISABLE_AUTOUPDATER=1`, `CRAB_STUDENT_NAME`, `CRAB_STUDENT_BRANCH`, and
  `TERM=xterm-256color` if `TERM` is missing;
- adds the `export K='v'` lines of `claude.env` and `secrets.env`, parsed with `shlex`, never sourced
  through a shell that echoes them.

The user attaches with `crab-student open NAME`, and a restart resumes the same conversation. If
the user exits Claude Code, the pane waits for Enter and then resumes it. The watchdog only
recreates the tmux session if the session itself is gone.

---

### 4.8 The watchdog: activity, busy, idle and time limit

`cs.py watch` runs once per container, guarded by a non-blocking `flock` on `D/watch.lock` (a second
copy exits 0 at once). When it starts it:

- reads `vast.env` into a private dict and removes `CONTAINER_*` from `os.environ`;
- loads `state.json`. If `boot` differs from the `booted` file (this container start is new), it sets
  `boot` from `booted`, `last_activity = last_tick = now`, `stopping = None`, and new baselines for
  the transcript size and CPU counter. A fresh start counts as activity.
- If the container start is not new (the watchdog itself was restarted), it keeps everything, so a
  restart can never reset the idle clock or the boot grace.

**Self-healing.** Nothing restarts a dead watchdog inside a running container except one rule:
`cs.py status`, which every `list` and `status` runs, spawns the watchdog detached if `setup-done`
exists and `watch.lock` is free. If nobody looks, the dead-man switch stops the machine within
45 minutes.

Then it loops every `TICK` (60 s):

```python
while True:
    try:
        cfg = load_config()                          # reread: set-limit may have changed it
        touch(D / "heartbeat")
        if st["stopping"]:
            vast_stop()                              # retried every tick until the container is gone
        else:
            ensure_worker_session(cfg)
            for action in tick(st, cfg, time.time(), Probe()):
                perform(action, st, cfg)             # notify / type NUDGE / type WARN / stop_machine
        save_state(st)
    except Exception as e:                           # the watchdog never dies of a bug; the dead-man is the net
        log(f"tick failed: {e!r}")
    time.sleep(TICK)
```

`tick` holds all the rules. It is pure apart from reading `worker.json`, and tests drive it with a
fake `Probe` and clock:

```python
CPU_BUSY_CORES = 0.25; GRACE = 300

def tick(st, cfg, now, probe):
    actions = []
    dt = min(max(now - st["last_tick"], 0), 2 * TICK); st["last_tick"] = now
    st["used_seconds"] += dt
    size, cpu = probe.transcript_bytes(), probe.cpu_seconds()
    cpu_busy = cpu is not None and st["cpu_seconds"] is not None and (cpu - st["cpu_seconds"]) >= CPU_BUSY_CORES * dt
    busy = size != st["transcript_bytes"] or cpu_busy or probe.gpu_busy()
    st["transcript_bytes"], st["cpu_seconds"] = size, cpu
    if busy:
        st["last_activity"], st["nudged_at"] = now, None
    w = read_worker()                                # {"state", "message"} or {}
    if w.get("state") != st["notified_state"] and w.get("state") in ("needs-you", "done", "failed"):
        actions.append("notify:" + w["state"])
    st["notified_state"] = w.get("state")
    idle, limit = now - st["last_activity"], cfg["limit_hours"] * 3600
    if w.get("state") == "needs-you" and idle >= cfg["idle_minutes"] * 30 and st["nudged_at"] is None:
        actions.append("nudge"); st["nudged_at"] = now
    if st["used_seconds"] < limit - 600:
        st["warned_limit"] = False                   # re-arms after an extend
    elif not st["warned_limit"]:
        actions.append("warn"); st["warned_limit"] = True
    if st["used_seconds"] >= limit and now - st["boot"] >= GRACE:
        actions.append("stop:time-limit")
    elif idle >= cfg["idle_minutes"] * 60:
        actions.append("stop:idle")
    return actions
```

**What "busy" means.** A tick is busy when any of these happened since the previous tick:

1. **The conversation grew.** The total size of `~/.claude/projects/*/*.jsonl` changed. It grows
   only when Claude Code (the worker, or a session the user started) actually processes a message.
2. **The container used CPU.** The container used at least 0.25 of a core on average. This is read
   from the container's own cgroup counter (`/sys/fs/cgroup/cpu.stat` `usage_usec`, or cgroup v1
   `cpuacct.usage`), never from `/proc/stat`, which shows the whole host's load. Idle Claude Code,
   tmux, sshd and the watchdog stay far below this. Training, data processing and installs are above it.
3. **A GPU was working.** Any GPU showed at least 10% utilization in `nvidia-smi
   --query-gpu=utilization.gpu --format=csv,noheader,nounits`. With no `nvidia-smi`, this counts as
   not busy.

A user who is attached but not typing makes none of these happen, so an open session does not keep
the machine up. A worker that is thinking, running tools, or has a background job crunching does
keep it up. One sample per minute is enough, because an idle stop needs `idle_minutes` of
consecutive quiet ticks.

**Nudge** (worker said `needs-you` and nothing happened for half of `idle_minutes`). The watchdog
types this into the pane, once per quiet stretch:
`crab-student: nobody has answered for 30 minutes. Choose what you think is best, write the choice and
your reasons in REPORT.md under "Decisions", run crab-status working, and continue.`
The answer counts as activity, so the idle clock starts again. A question nobody answers therefore
costs at most half of `idle_minutes` of waiting, and the worker never stalls forever.

**Warning** (10 minutes before the limit):
`crab-student: about 10 minutes of machine time are left before the time limit (6h00m). Commit and
push your work and bring REPORT.md up to date now. The machine will stop itself.`

**Typing to the worker** (`type_to_worker`, also used by `crab-student send`):
`tmux load-buffer -b cs -` with the text on stdin, then `tmux paste-buffer -p -d -b cs -t worker`
(a bracketed paste, so a multi-line message stays one message), sleep 0.5 s, then
`tmux send-keys -t worker Enter`. Claude Code queues typed messages while it works.

**Stopping** (`stop_machine(st, reason)`):

1. Set `st.stopping` and `st.stop_reason`, and save the state, so `status` shows `stopping`.
2. `notify("stopped", ...)`.
3. `save(reason)` (4.10), limited to 3 minutes.
4. `vast_stop()`: `PUT {CS_VAST_URL}/api/v0/instances/{CONTAINER_ID}/ {"state": "stopped"}` with
   `Authorization: Bearer {CONTAINER_API_KEY}`, timeout 30 s.

If the call fails, the next ticks retry it. The watchdog keeps its heartbeat going while it
retries, so the dead-man switch does not fire. The machine side never destroys anything.

### 4.9 The dead-man switch

The onstart line (3.5) starts a bash loop at every container start, before any of our files exist.
Every 60 s it takes the newer of `booted` (this start) and the mtime of `heartbeat` (touched every
tick by the watchdog). If that time is more than 2,700 s (45 minutes) ago, it calls Vast stop with
the container's own key. This covers four cases:

- The upload never happens (an interrupted launch).
- Setup fails and the local side cannot destroy the machine (for example, the laptop went offline).
- The watchdog is never started.
- The watchdog hangs or dies.

In each of these the machine stops within about 45 minutes. The loop repeats the stop call every
minute until the container is gone, which does no harm. It does not try to save, because the
machinery that saves is what failed. The worker's own frequent pushes are the protection for its work.

### 4.10 Save before stopping

`save(reason)` runs on the machine before every stop: idle, time limit, `crab-student stop`, and
`delete` (as part of `check`).

```python
def save(reason):
    repo, branch = cfg["repo_dir"], cfg["branch"]
    busy_git = any((repo / ".git" / f).exists() for f in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))
    on_branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False) == branch
    if on_branch and not busy_git:
        git("add", "-u")                                     # tracked changes, including deletions
        for f in untracked_files():                          # git ls-files --others --exclude-standard -z
            (skipped if size(f) > 10 MB else added).append(f)    # big files never go into git by autosave
        git("add", "--", *added)
        if staged():                                         # git diff --cached --quiet exits 1
            if git("commit", "-q", "-m", f"crab-student: autosave ({reason})", check=False) fails:
                git("reset", "-q"); error = "not committed: " + hook_message   # e.g. a secret was found
    git("push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{branch}", check=False, timeout=120)
    return {"committed": n, "pushed": ok, "skipped": skipped, "dirty": dirty(), "unpushed": ahead(), "error": error}
```

"What can safely be saved" therefore means:

- tracked changes, plus untracked files of 10 MB or less that are not ignored;
- nothing while a merge or rebase is in progress;
- nothing on a branch other than the worker's own;
- never a commit the pre-commit hook refuses (secrets, files over 50 MB).

The push goes through the pre-push hook like any other push.

### 4.11 Where the branch starts (the user's repo and the workspace)

**Default: the user's current repo.**

- `local_repo(--repo or cwd)` finds the top level, then the GitHub remote: `origin` if it points to
  github.com, else the first remote that does. It accepts `git@github.com:o/r(.git)`,
  `ssh://git@github.com/o/r` and `https://github.com/o/r(.git)`.
- `check_repo(slug, branch)` uses `gh api repos/<slug>`. `permissions.admin` must be true, because a
  deploy key needs admin rights. If not, it exits 1: `You need admin rights on o/r so crab-student can
  add a deploy key for the worker. Ask an owner, or run the task in your workspace repo with --no-repo.`
- It then calls `gh api repos/<slug>/git/matching-refs/heads/agent/<name>`, and an exact match exits 2:
  `branch agent/NAME already exists on o/r. Pick another name.`

**Local commits and uncommitted changes are carried over** (decision in section 5).
`snapshot(repo)` builds the starting commit without touching anything of the user's:

```
head      = git rev-parse HEAD
changed   = git diff --name-only HEAD                  # tracked files with staged or unstaged changes
untracked = git ls-files --others --exclude-standard   # reported, never carried
if changed:   # a temporary index, so the user's index, working tree, branches and stash are untouched
    GIT_INDEX_FILE=<tmp> git read-tree HEAD
    GIT_INDEX_FILE=<tmp> git add -u
    tree = GIT_INDEX_FILE=<tmp> git write-tree
    start_sha = git commit-tree <tree> -p <head> -m "crab-student: uncommitted changes at launch"
else:
    start_sha = head
```

After the rent, `create_branch` pushes that commit to GitHub with the new deploy key:
`git -C <top> push <git_url(slug)> <start_sha>:refs/heads/agent/<name>`, with
`GIT_SSH_COMMAND='ssh -i "<key>" -o IdentitiesOnly=yes -o UserKnownHostsFile="<github_known_hosts>" -o StrictHostKeyChecking=yes'`
(forward-slash paths). This uses no user credentials and no prompts, which matters because the
background launcher cannot answer one. If the user's own commits are not on GitHub yet, they go up
as part of `agent/<name>` only. The push is tried 3 times, 3 s apart, because a new deploy key can
take a moment to work.

The launch says exactly what happened, both in the progress output and as warnings for the launcher:

```
repo:    me/proj - agent/ablation-1 starts from 1a2b3c4 (your HEAD on main)
         + 2 local commits not on GitHub + uncommitted changes to 2 files (train.py, configs/a.yaml)
Warning: not carried over: 1 untracked file (notes.txt). Commit it and launch again if the worker needs it.
```

**`--no-repo`: the workspace repo.** In `prepare_repo` (after the rent, so a pending launch writes nothing), `ensure_workspace(login, cfg["workspace_repo"])` creates
`<login>/crab-student-work` if it is missing, with
`gh api user/repos -f name=... -F private=true -F auto_init=true`. `agent/<name>` is created
through the API (`POST repos/<slug>/git/refs`) from the default branch's head. The worker gets its
own deploy key on that repo, the same as for any other repo.

### 4.12 "Done" must mean pushed: crab-status and the git hooks

`crab-status STATE "message"` writes `worker.json`. The states are `working`, `needs-you`, `done`
and `failed`, and the message is trimmed to 120 characters on one line. With no arguments it prints
the current status, the machine time used and left, and the idle rule.

**Before `done` is accepted**, `cmd_declare` checks the branch:

1. `REPORT.md` exists at the repo root and is tracked.
2. `git status --porcelain` is empty: no uncommitted changes and no untracked files that are not ignored.
3. After `git fetch -q origin <branch>` (30 s; if the fetch fails, the local tracking info is used),
   HEAD equals `origin/<branch>`.

If a check fails, the command exits 1 and `worker.json` is not changed:

```
not done yet: 2 files have uncommitted changes (a.py, b.py) and 1 commit is not pushed.
Commit and push them (git push), then run crab-status done again.
```

Small models that say "done" too early get this error in their own Bash output and fix it. The
watchdog also shows `dirty`/`unpushed` in `status` at every tick, and `list` shows `done*`
(`not all pushed`) if the tree changes after `done`.

**Hooks** (in `D/hooks`, set as `core.hooksPath`):

- **`pre-commit`** refuses a commit when either of these is true:
  - A line added in `git diff --cached -U0` contains the value of the Claude token, of any value in
    `secrets.env` of 8 characters or more, or of the deploy key's first base64 line. The message
    names the secret and the file, never the value: `crab-student: train.py contains the value of
    secret HF_TOKEN. Use the environment variable instead.`
  - A staged file is over 50 MB: `data/big.bin is 1.2 GB: keep big files out of git (add it to
    .gitignore; put outputs in /root/outputs).`
- **`pre-push`** reads `<local ref> <local sha> <remote ref> <remote sha>` lines. It refuses any
  remote ref other than `refs/heads/<branch>`, and it refuses deleting that branch:
  `crab-student: this worker may push only to agent/NAME, not refs/heads/main.`

The worker's Claude Code settings deny the obvious ways around the hooks (6.5). GitHub cannot limit a
deploy key to one branch, so these checks are on the client side (section 9).

### 4.13 Delete's safety check

```python
def delete(name, force):
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] == "pending": core.remove(name); return
        inst = find_instance(rec)              # by instance_id, or by exact label for a renting record
        if inst and rec["phase"] == "ready" and not force:     # only a finished launch can hold work
            if inst["actual_status"] != "running":
                raise CrabError(f"{name} is stopped, so crab-student cannot check it for unsaved work. "
                                f"Start it (crab-student start {name}) and delete again, or delete anyway with --force.", 3)
            res = machine.remote(name, ["check"], timeout=240)   # unreachable -> CrabError(3) "... use --force"
            if problems := describe(res):
                raise CrabError(refusal(name, problems), 3)
        if inst: vast.destroy(inst["id"])
        cleanup(rec)   # deploy key removed; branch removed only if still at start_sha; known_hosts; record files
```

`cs.py check` first runs `save("delete")`, then reports:

| Field | What counts |
|---|---|
| `unpushed` | Commits on the branch not on `origin` (after `git fetch`); for example, the push failed |
| `dirty` | Files still uncommitted after the save (too big, refused by a hook, a rebase in progress) |
| `outputs` | Any file under `/root/outputs` (count, total size, first 5 paths) |
| `large_files` | Untracked or ignored files of 100 MB or more in the repo, from `git ls-files --others [--ignored] --exclude-standard`, not under `.git`, `node_modules`, `.venv`, `venv`, `__pycache__` or `.cache` |

If anything is found, delete refuses with exit 3 and lists it, with a copy command:

```
error: ablation-1 has work that would be lost:
  - /root/outputs: 12 files, 5.1 GB (checkpoints/best.pt, ...)
  - large files outside git: runs/ckpt-last.pt (2.1 GB)
Copy what you need first, for example:
  scp -F ~/.crab-student/ssh/config -r crab-student-ablation-1:/root/outputs ./ablation-1-outputs
Then delete again, or delete anyway with: crab-student delete ablation-1 --force
```

When a machine cannot be checked (stopped, unreachable), `--force` is required. A launch that never
finished (`renting`, `booting`, `setup`, `failed`) holds no work, so it is destroyed without a check.
After a successful delete:

```
Deleted ablation-1: machine 987654 destroyed, deploy key removed.
Branch agent/ablation-1 stays on me/proj (7 commits).
```

### 4.14 open, open --all, VS Code

**Before opening**, the worker must be `ready` and Vast must show it `running`. Otherwise:
`ablation-1 is stopped. Start it first: crab-student start ablation-1` (exit 1). The address is
refreshed from Vast and the ssh config rewritten, because ports can change after a restart.

The remote command is always `crab-attach`, a script setup installs in `/usr/local/bin`. It runs
`cs.py attach`, which execs `tmux attach -t worker`, or prints `The worker's session is not running
(see crab-student status NAME). Opening a shell.` and execs `bash -l`. Keeping it to one plain word
avoids quoting problems in ssh, Windows Terminal and tmux.

**`open NAME`**:

- If stdin and stdout are terminals, it prints `Attaching to ablation-1 (detach: Ctrl-b d - the
  worker keeps running).`, then runs `ssh -F cfg -t -o BatchMode=no crab-student-NAME crab-attach` as
  a child process and returns its exit code. A closed terminal, a dropped connection or a sleeping
  laptop only ends the ssh client. tmux keeps the session, and running the command again reattaches.
- If there is no terminal (for example, Claude Code ran it) and this is Windows with `wt` on PATH,
  it opens a new Windows Terminal window running the same ssh command and prints `Opened ablation-1
  in a new Windows Terminal window.`
- Otherwise it exits 2: `open needs a terminal: run "crab-student open NAME" in your terminal.`

**`open --all`** opens every worker whose machine is running. With none, it exits 1.

- **Windows** needs `wt`. It opens one new window with two columns: the left column gets
  `ceil(n/2)` panes and the right column the rest. The order of operations is:
  1. `new-tab --title L1 <ssh L1>`, then `split-pane -V --title R1 <ssh R1>`.
  2. `move-focus left`. Then, for each further left pane `i` of `m`,
     `split-pane -H --size (m-i+1)/(m-i+2) --title Li <ssh Li>`, which gives equal heights.
  3. `move-focus right`, and the same for the right column.

  Each of these is a separate argument group joined by `;` in one `wt -w new ...` call.
- **macOS and Linux** need a local `tmux`. The command does four things:
  1. Kills any old session: `tmux -L crab-student kill-session -t workers`.
  2. Creates the session with its own config: `prefix C-a`, so the remote Ctrl-b goes through to the
     worker's tmux; `mouse on`, so a click picks a pane; and pane border titles showing worker names.
  3. Runs `split-window` for each worker's ssh command, with `select-layout tiled`.
  4. Attaches with `tmux -L crab-student attach -t workers`. Without a terminal it exits 2.
- When `wt` or `tmux` is missing, the error names what to install and suggests `crab-student open
  NAME` one at a time.

**`--vscode`** (with NAME or `--all`): `code --remote ssh-remote+crab-student-NAME /root/work/<repo>`.
It works because init put `Include ~/.crab-student/ssh/config` at the top of `~/.ssh/config`, and
`/etc/profile.d/crab-student.sh` gives VS Code's login-shell environment Python and Claude on
`PATH`. If `code` is not found, the error says how to add it to PATH.

### 4.15 Display states (`list`, `status`)

`workers.views()` gathers the data in three steps:

1. One `vast.instances()` call, used for every record with an instance.
2. For each `ready` record whose instance is `running`, one `cs.py status` over ssh. These run in
   parallel (at most 8 at once), with `ConnectTimeout=8` and a 15 s overall timeout.
3. Under each record's lock (skipped when the lock is taken), `last_seen` and the address are
   updated. `display_state` then maps the result:

| Condition (checked in this order) | STATE | MESSAGE |
|---|---|---|
| phase `pending` | `approval` | `waiting for approval: <reason>` |
| phase renting/booting/setup, lock held | `launching` | `renting` / Vast `status_msg` / `setting up` |
| phase `renting`, lock free | `rent unknown` | `run crab-student start NAME to look for the machine` |
| phase booting/setup, lock free | `interrupted` | `launch interrupted: crab-student start NAME continues it` |
| phase `failed` | `launch failed` | the error |
| no instance on Vast | `gone` | `the machine no longer exists on Vast` |
| Vast not running, intended running | `starting` | Vast `status_msg` |
| Vast not running | `stopped` | stop reason if known (`stopped by you`, `idle stop`, `time limit`) + last message |
| running, ssh failed | `unreachable` | last known message |
| `status.stopping` | `stopping` | the reason |
| worker `needs-you` | `needs you` | the question |
| worker `done`, dirty or unpushed | `done*` | message + ` (not all pushed)` |
| worker `done` | `done` | message |
| worker `failed` | `failed` | message |
| no worker status yet | `starting` | `worker starting` |
| idle 10 min or more | `idle` | `idle 23m - <message>` |
| otherwise | `working` | message |

A pending `start` or `extend` appends ` (waiting for approval to <action>)` to the message.

### 4.16 list --watch alerts

`list --watch` redraws every 30 s. It clears the screen with `ESC[2J ESC[H`, calling `os.system("")`
once on Windows to turn on VT processing, then prints the header
`crab-student - every 30 s - Ctrl-C to quit - 14:05:31`, the table, and the last 8 alerts.

An alert is raised when a worker's STATE changes, compared with the previous refresh, into one of
`needs you`, `done`, `done*`, `failed`, `stopped`, `launch failed` or `gone`. It prints a line and
rings the terminal bell (`\a`) once for that refresh. The first refresh does not alert, because
those states are already visible in the table.

```
[14:02] refactor-db needs you: Drop the legacy_users table? It has 3 rows.
[14:05] data-prep is done: cleaned 1.2M rows -> crab-student report data-prep
[14:05] big-train stopped: idle stop
```

Ctrl-C exits 0.

### 4.17 Notifications (`notify_url`)

The machine sends notifications for four events: the worker entering `needs-you`, `done` or
`failed`, and the watchdog stopping the machine. A user stop does not notify, and the dead-man switch
cannot. The payload depends on the URL's host:

| URL | Request |
|---|---|
| host `ntfy.sh` (or any host containing `ntfy`) | POST, body = text, headers `Title: crab-student: NAME`, `Tags: warning` / `white_check_mark` / `stop_sign` |
| `hooks.slack.com` | POST JSON `{"text": "crab-student: NAME needs you: ..."}` |
| `discord.com` or `discordapp.com`, path `/api/webhooks/` | POST JSON `{"content": "..."}` |
| anything else | POST JSON `{"worker": NAME, "event": "needs-you", "message": "...", "text": "crab-student: NAME needs you: ..."}` |

The texts are `NAME needs you: <msg>`, `NAME is done: <msg>`, `NAME failed: <msg>` and
`NAME stopped: idle for 60 min` / `NAME stopped: time limit (6h00m) reached`.

Sending is best effort: urllib with a 10 s timeout, and failures logged as `notify failed: HTTP 404`,
never with the URL, which may contain a secret. `notify_url` and `idle_minutes` are copied to the
machine at launch, so a change reaches workers launched after it (section 5).

### 4.18 init

Each step checks first and does nothing if it is already done, so re-running init only asks for
what is missing:

1. **Vast key.** It reads `vast_key_file()`. If the file is missing, init asks with `getpass`, checks
   the key with `GET /users/current`, and writes it (0600, creating the folder). It prints the
   account's email and credit.
2. **GitHub.** It requires `gh` on PATH. If `gh auth status` fails, init runs `gh auth login`
   interactively. It prints the login.
3. **SSH key.** It creates `~/.crab-student/ssh/id_ed25519` with ssh-keygen and writes
   `github_known_hosts`. It adds `Include ~/.crab-student/ssh/config` as the first line of
   `~/.ssh/config`, creating the file if needed, and tells the user in one line.
4. **Claude token.** If none is stored (or with `--claude-token`), init explains: `In another
   terminal, run: claude setup-token. Paste the token (sk-ant-oat01-...) here:`. It reads the token
   with getpass, checks its format, and stores it in the keyring.
5. **Claude Code integration.** Always: it writes `SKILL.md` and merges the permission rules (6.3)
   into `~/.claude/settings.json`, keeping every existing entry and adding none twice.
6. **Tools.** It checks `ssh`, `ssh-keygen`, `git` and `gh`, which are required. `wt` (Windows) or
   `tmux` (macOS/Linux) is optional and only for `open --all`.

It ends with `Ready. In Claude Code, ask for a task to run on a GPU, e.g. "run the ablation on a
GPU with crab-student".` (sample in section 7.6).

### 4.19 The other commands

- **`stop NAME`**:
  - running: `remote(["save", "--reason", "user"], timeout=200)`, then `vast.set_state(id,
    "stopped")`, then `last_seen.stop_reason = "user"`. It prints `Saved: 3 files committed and
    pushed.` (or the save's error) and `Stopped ablation-1. Its disk is kept (storage is still
    billed); start it again with crab-student start ablation-1.`
  - already stopped: says so, exit 0.
  - pending: points to `decline`.
- **`start NAME`**:
  - unfinished or failed launch: `bring_up` (with the machine-count gate for a failed one).
  - `ready` and stopped: time-limit check, the gate, `vast.set_state(id, "running")`, `wait_boot`
    without destroying, the address, `set-limit` with the record's limit, `limit_pushed = true`.
    If `set-limit` reports `used_hours >= limit_hours` (the last value seen locally was stale), it
    stops the machine again at once and exits 3 with the time-limit message. At most one boot is
    wasted, and the machine never runs past its limit.
  - already running: says so and prints the result line.
- **`extend NAME HOURS`**: section 4.2 (`HOURS > 0`).
- **`send NAME [TEXT...]`**: the text comes from the arguments, or from stdin if there are none;
  it cannot be empty. It runs `remote(["send"], input=text)`. It needs a running machine.
- **`report NAME`**: prints `REPORT.md` from `agent/<name>` on GitHub (`gh api -H "Accept:
  application/vnd.github.raw" repos/<slug>/contents/REPORT.md?ref=agent/<name>`). With no report yet
  it exits 1: `No REPORT.md on agent/NAME yet. The worker writes it as it goes.` It works for stopped
  workers too, because it reads GitHub, not the machine.
- **`pull NAME [--repo PATH]`**:
  - user repo: the repo at PATH (default cwd) must have a remote for the record's slug. It runs
    `git fetch <remote> refs/heads/agent/NAME:refs/remotes/<remote>/agent/NAME`, then tries a
    fast-forward-only update of the local branch `agent/NAME`
    (`git fetch <remote> agent/NAME:agent/NAME`). If that is refused (checked out, or diverged), the
    local branch is left alone and the output says so. It prints `Fetched agent/NAME (9f8e7d6, 7
    commits). Look: git log main..agent/NAME - try: git switch agent/NAME`. It never touches the
    checkout.
  - workspace: it clones `<login>/crab-student-work` into `~/crab-student-work` if missing (with
    `gh repo clone`), then does the same fetch there, and prints the path.
- **`decline NAME`**: section 4.3.

---

## 5. Decisions

These are the choices the requirements left open, each with its reason.

| # | Decision | Reason |
|---|---|---|
| 1 | No local process between commands: records are files with per-record OS locks; views are computed on demand from Vast and one ssh call per machine | The requirements forbid a service. Files and locks are the least code that stays correct when two commands overlap |
| 2 | **Local changes are carried over.** `agent/<name>` starts from the user's HEAD plus any local commits not on GitHub, plus uncommitted changes to tracked files as one snapshot commit. **Untracked files are not carried** and are listed as a warning | The worker starts from what the user sees, without asking them to push work in progress to a shared branch. Untracked files are often junk or secrets (`.env`), so they are never uploaded silently |
| 3 | The starting commit is pushed from the user's computer **with the worker's deploy key**, not the user's credentials | No passphrase or credential prompt can block the background launcher, and the push can create only what the key can |
| 4 | Picking rule of 4.1: GPU tasks drop offers more than 20% worse in dlperf/$ than the best within 1.5x the cheapest price; among offers within 10% of the lowest price, fastest download wins; ties go to price, then id | Deterministic, testable, and it matches "cheapest, but better price-for-speed when the difference is clear" and "prefer faster downloads among similar prices" |
| 5 | Fallbacks: up to 2, each on a different machine, at most 1.25x the primary's price. A fallback keeps the budget and gets a shorter time limit | A boot failure is often the host's fault. The approved or automatic budget never grows silently |
| 6 | Budget = `ceil_cents(dph x max(2 x hours, 2 h))`. An explicit `--budget` filters offers by `dph x hours <= budget` | Matches "about twice the estimated cost", with headroom for short tasks |
| 7 | One gate, three actions: `launch`, `start` and `extend` record a `pending` action when over a limit, and `approve` performs it | One code path and one "ask" command for every way of spending more |
| 8 | The worker is interactive Claude Code in tmux, started with `--session-id` and resumed with `--resume` | The user sees and types to the very session that does the work, and a restart resumes it natively. No headless protocol to maintain |
| 9 | "Busy" = the conversation files grew, or the container's cgroup CPU >= 0.25 core, or any GPU >= 10%, measured once a minute | Each signal is cheap and local to the container. An attached but unused session triggers none of them |
| 10 | An unanswered `needs-you` gets a "decide yourself" nudge after half of `idle_minutes` | Meets "if nobody answers, choose sensibly" without timers inside Claude Code, and costs at most half the idle time |
| 11 | `crab-status done` refuses unless REPORT.md is tracked, the tree is clean and the branch is pushed | Fixes the "done without pushing" problem where it happens, in the worker's own tool output |
| 12 | Autosave commits tracked changes plus untracked files of 10 MB or less, never during a merge or rebase, never on another branch | "What can safely be saved" without pushing datasets or checkpoints to GitHub |
| 13 | A setup failure destroys the machine and fails the launch (exit 5). There is no automatic retry on the next offer; `start NAME` retries | Setup failures are usually not the host's fault (token, repo, network to GitHub), so blind retries would spend money. The requirements allow destroying here |
| 14 | An unknown rent outcome with no label match within 2 minutes stops the launch. It does not move on to other offers | Moving on could rent twice. "Never rent twice" beats finishing the launch automatically |
| 15 | REPORT.md lives at the root of the worker's branch; `report` reads it from GitHub | It is visible on GitHub and works when the machine is stopped or deleted. The committed report is what counts |
| 16 | A `send NAME` command types a message to the worker | Requirement 2.7: the user's Claude Code can steer a worker as well as watch it. About 15 lines |
| 17 | `open` without a terminal opens a Windows Terminal window on Windows; elsewhere it asks the user to run it. `open --all` uses `wt` on Windows and a local tmux on macOS/Linux | Covers the Windows-first case without per-OS terminal launchers |
| 18 | `notify_url` and `idle_minutes` are copied to the machine at launch; later changes apply to new workers | Keeps the machine config simple. `extend` is the only setting that changes a running worker |
| 19 | Money spent is shown as used machine hours x $/hr, marked approximate | Vast has no reliable per-instance total. The watchdog's counter survives restarts |
| 20 | The deploy key stays in `~/.crab-student/workers/` (0600, made by ssh-keygen) until `delete` | A retried setup can upload it again without making a new GitHub key |
| 21 | Setup checks the Claude token with one tiny `claude -p` call (haiku) | Tokens expire after a year. Catching it at launch beats a worker silently sitting at an auth error |
| 22 | `--no-repo` workers use one private `<login>/crab-student-work` repo, one branch each, starting from its README commit | One repo to find everything, and no new repo per task |
| 23 | `--gpus 0` (the default) is a CPU task: GPU hosts are allowed and rented with `NVIDIA_DISABLE_REQUIRE=1` | Requirement fact: cheap GPU hosts often beat CPU-only hosts |
| 24 | `regions` takes ISO country codes and six continent names | Enough for latency choices with a 6-line table |
| 25 | The launcher passes the brief on stdin (a quoted heredoc); `--brief-file` is for people | No temporary file for the background launcher to write, which would need an edit permission |
| 26 | The launcher runs `launch` in the foreground with the Bash tool's 10-minute timeout; if it is cut off, it runs `start NAME`, which continues the same launch | Needs no special harness feature, and `bring_up` is resumable anyway |
| 27 | `delete` removes `agent/<name>` only if it still points at the launch's starting commit | A failed launch leaves no stale branch blocking the name. A branch with work is never touched |
| 28 | `pull` updates `refs/remotes/<remote>/agent/<name>` and fast-forwards a local `agent/<name>` only if that is safe | "Without touching their checkout", and the user's own commits on that branch are never overwritten |
| 29 | The time limit is not enforced during the first 5 minutes after a boot | Lets `start` push a limit raised while the machine was stopped. The overshoot is bounded and happens only right after a user action |
| 30 | `min_budget_hours` defaults to 2 and `min_download_mbps` to 200 | Short tasks get room for setup mistakes, and the image pull stays quick on most hosts |

---

## 6. Claude Code integration

Three Markdown files ship in the package. `data/SKILL.md` is copied to
`~/.claude/skills/crab-student/SKILL.md` by `init`. `data/launch.md` is printed by `crab-student guide
launch` at run time. `remote/worker.md` becomes the worker's `~/.claude/CLAUDE.md`.

### 6.1 The skill (`SKILL.md`, at most 120 lines)

```markdown
---
name: crab-student
description: Run long or heavy tasks (training, experiments, data processing, big refactors, anything
  slow) on a rented Vast.ai machine, GPU or CPU, with a remote Claude Code worker, and manage those
  workers - status, reports, results, steering, stopping, deleting. Use when the user asks to run
  something on a GPU or on a remote, rented or cloud machine, mentions Vast or crab-student, or names a
  crab-student worker.
---
```

Outline of the body:

1. **What it is**, in 3 lines: workers are Claude Code on rented machines, on branch `agent/<name>`.
   They report through `REPORT.md`. Money is bounded by a budget, a time limit, an idle stop and an
   approval gate.
2. **Launching.** Do not do the estimation yourself. Start a background agent with the Agent tool,
   `run_in_background: true`, and this prompt: `You are the crab-student launcher. Run "crab-student
   guide launch" and follow it exactly. The task, in the user's words: <...>. Context: <repo path,
   relevant files, data, secrets, deadlines or budget the user mentioned>.` Keep talking with the
   user. When the launcher finishes, show the user the brief it gave the worker and the result line
   and `Warning:` lines, exactly as returned.
3. **If the result says approval is needed**, tell the user the amount and why, and ask. Only after an
   explicit yes in this conversation run `crab-student approve NAME --json`, with a 10-minute timeout.
   If it is cut off, run `crab-student start NAME --json`. `crab-student decline NAME` if they say no.
4. **Managing.** A table of user intents and commands:

   | The user wants | Command |
   |---|---|
   | overview | `list --json` |
   | how one is doing | `status NAME --json` |
   | its report | `report NAME` |
   | its results locally | `pull NAME` |
   | to tell it something | `send NAME` with the text on stdin |
   | to stop it | `stop NAME` |
   | to start it again | `start NAME --json` |
   | more time | `extend NAME HOURS --json` |

   Summarize the output in plain words. Do not paste tables unless asked.
5. **Watching.** Tell the user to run `crab-student open NAME` (or `open --all`, or `open NAME
   --vscode`) in their own terminal. The attach is interactive, and detaching with Ctrl-b d leaves
   the worker running. On Windows, running it yourself opens a new Windows Terminal window.
6. **Deleting.** Always confirm with the user first, and suggest `pull` and reading the report
   before. If delete refuses (exit 3), show its list and let the user decide about `--force`. Never
   add `--force` on your own.
7. **Never, without the user's explicit OK in this conversation:** `approve`, `decline`, `delete`,
   `config KEY VALUE`, `init`. The user runs `secret set NAME` themselves in a terminal, and you
   never see secret values. The permission prompts enforce this too.
8. **Exit codes:** 0 ok, 1 error, 2 usage, 3 needs approval / refused by a limit / would lose work,
   4 no machine fits, 5 setup failed. Error text on stderr says what to do.
9. **Windows note:** use stdin for text, and never pass arguments that start with `/` (Git Bash
   rewrites them).

### 6.2 The launcher's procedure (`guide launch`, at most 130 lines)

1. **Your limits.** You run in the background. You cannot ask the user anything or answer a
   permission prompt. Use only:
   - the Read, Grep and Glob tools;
   - read-only git (`git status`, `git log`, `git diff`, `git remote -v`);
   - `crab-student guide|offers|list|secret list|launch|start|status`.

   Never run `approve`, `config`, `secret set`, `delete` or `init`. Never put a secret's value in the brief.
2. **Understand the task.** Read the code, README and configs it involves. Find the data, its size
   and where it comes from, the dependencies, and how success is measured.
3. **Choose the repo.**
   - The default is the current repo. It needs a GitHub remote, and the user needs admin rights on it.
   - Use `--no-repo` when the task has nothing to do with this repo, or when there is no GitHub repo.
   - Check `git status`. Uncommitted changes to tracked files are carried over automatically.
     Untracked files are not, so if the task needs them, add a warning line; never commit for the user.
4. **Estimate.** A compact heuristics table:
   - GPU or CPU: CPU unless the task uses CUDA, trains or runs neural models.
   - GPU memory:
     - inference in fp16/bf16: about 2 GB per billion parameters, +20%;
     - LoRA: about 1.5x inference;
     - full fine-tuning with Adam in mixed precision: about 16 GB per billion parameters;
     - small models (CNNs, tabular): 8-16 GB.
   - `--gpus` above 1 only if the code already supports multiple GPUs.
   - `--gpu-models` only for a hard need, for example bf16 needs Ampere or newer:
     `"A100,H100,L40,A6000,4090,3090,L4"`.
   - CPU cores: 4-8, or 16+ for heavy data processing.
   - RAM: at least 16 GB, and 2x what is held in memory.
   - Disk: 20 GB for the image and environment, plus data, plus checkpoints, plus 30%.
   - `--min-down 500` when more than 50 GB will be downloaded.
   - Hours: the expected run time plus 30 minutes of setup, rounded up. Say in the brief how sure you are.
5. **Name.** 3-40 characters from a-z, 0-9 and "-", descriptive (`ablation-lr`, `refactor-db`),
   and not in `crab-student list --json`.
6. **Secrets.** `crab-student secret list` shows the stored names. Attach the ones the task needs with
   `--secret NAME`. If a needed one is missing, do not launch. Return `Needs secret HF_TOKEN: run
   crab-student secret set HF_TOKEN in a terminal, then ask me to launch again.`
7. **Write the brief** from this template (headings, one line each on what goes there):
   Task (one line), Goal, Context (repo, files, data location and size), What to do (steps,
   experiments, parameters, scope), Deliverables and success criteria (evidence expected), Constraints
   (time estimate, where big outputs go, what not to change), Secrets available (names only),
   Preferences the user already stated.
8. **Optionally sanity-check the price** with `crab-student offers <same flags> --hours H`.
9. **Launch:**

   ```
   crab-student launch NAME --hours H [flags] [--secret NAME...] --json <<'BRIEF'
   <brief>
   BRIEF
   ```

   Use the Bash tool with a 600000 ms timeout. If the call is cut off or times out, run
   `crab-student start NAME --json`, which continues the same launch, until it returns. Never run
   `launch` twice for one task, because that makes a second worker.
10. **Return exactly:** the brief, a blank line, the `result_line`, then one `Warning: <text>` line
    for each entry of `warnings`.
    - Exit 3 (approval): the same format; the result line says what to approve and why. Never approve.
    - Exit 2 with "already exists": pick another name once and retry.
    - Exit 1, 4 or 5: the brief, then `Launch failed: <error>` and the suggestion from the error.

### 6.3 Permission rules (merged into `~/.claude/settings.json` by init)

```python
ALLOW = ["Bash(crab-student guide:*)", "Bash(crab-student offers:*)", "Bash(crab-student launch:*)",
         "Bash(crab-student list:*)", "Bash(crab-student status:*)", "Bash(crab-student report:*)",
         "Bash(crab-student pull:*)", "Bash(crab-student send:*)", "Bash(crab-student open:*)",
         "Bash(crab-student stop:*)", "Bash(crab-student start:*)", "Bash(crab-student extend:*)",
         "Bash(crab-student secret list:*)"]
ASK   = ["Bash(crab-student approve:*)", "Bash(crab-student decline:*)", "Bash(crab-student delete:*)",
         "Bash(crab-student config:*)", "Bash(crab-student init:*)",
         "Bash(crab-student secret set:*)", "Bash(crab-student secret delete:*)"]
```

Why each group is where it is:

- **Allowed:** reading, and actions that are in budget by construction. `launch`, `start` and
  `extend` check the gate themselves and only record a pending request when over a limit. `stop`
  saves first and keeps the disk. `pull` only fetches, and `send` only types.
- **Ask:** actions that spend beyond the gate (`approve`), destroy or discard (`delete`, `decline`,
  `secret delete`), or change settings (`config`, `init`, `secret set`).

Claude Code checks ask rules before allow rules, so these rules hold even when the user has a broad
allow rule such as `Bash(crab-student:*)`. Reading config also asks, which is acceptable: the
launcher never needs it. Tests pin that every command which spends, destroys or changes settings is
in `ASK` and none is in `ALLOW`.

### 6.4 The worker's rules (`worker.md` -> `~/.claude/CLAUDE.md`, at most 130 lines)

Outline:

1. **You**: a crab-student worker called `{{name}}`, alone on a rented machine (root, disposable), with
   the task in `/root/crab-student/BRIEF.md`. Your repo is `{{repo_dir}}` on branch `{{branch}}`. The
   user may attach to this session at any time and type to you. Messages starting with
   `crab-student:` come from the machine's watchdog.
2. **Work like a capable colleague**:
   - Decide ordinary things yourself.
   - Ask only about important or hard-to-reverse choices: deleting data you did not create, changing
     the goal, anything costing much more time than estimated.
   - To ask, run `crab-status needs-you "<the question>"`, then end your turn with the question and
     your default choice.
   - If you are told nobody answered, choose, record it under Decisions in REPORT.md, run
     `crab-status working`, and continue.
3. **Status**: `crab-status working "<what you are doing>"` at the start and at every phase change.
   End with `crab-status done "<one-line result>"` or `crab-status failed "<why>"`. `crab-status`
   with no arguments shows machine time used and left.
4. **Git**:
   - Commit and push after every meaningful step with a plain `git push`. Unpushed work can be lost
     when the machine stops.
   - Push only `{{branch}}`. Never force-push other branches, change remotes, use `--no-verify` or
     change hooks.
   - Keep big files (over 50 MB), datasets, checkpoints, caches and virtualenvs out of git, using
     `.gitignore`.
   - Put big outputs in `/root/outputs/`.
   - Small results (tables, plots, JSON under 5 MB) go in git under `reports/`.
5. **Long jobs**:
   - A Bash call is cut off after about 10 minutes, so run anything longer as
     `nohup <cmd> > logs/<job>.log 2>&1 &` and record the PID.
   - Check on it with `tail`, and sleep at most 9 minutes per call.
   - Save checkpoints so a restart can resume.
   - Check the GPU with `nvidia-smi`.
6. **Environment**: Python is `/venv/main/bin/python` (with pip), and `apt-get` works. Secrets are
   available as environment variables: `{{secrets}}`. Never print, log or commit their values.
7. **Time and money**:
   - The machine stops itself at its time limit and after `{{idle_minutes}}` minutes with nothing
     happening. You get a warning 10 minutes before the limit.
   - Never stop or destroy the machine yourself, and never call the Vast API.
   - If the task will clearly not fit in the time, say so early with `needs-you`.
8. **Notes and report**: create `REPORT.md` at the repo root from the template below at the start.
   Keep its Log section current, a line per step. Fill in the rest as results come in.
9. **Finishing**: complete REPORT.md, commit, push, then run `crab-status done "..."`. The command
   refuses if anything is uncommitted or unpushed; fix that and run it again.
10. **After a restart** (you get a note): background jobs are gone. Check git, logs and checkpoints,
    then continue from where you were.

REPORT.md template (included verbatim in `worker.md`):

```markdown
# {{name}}: <task in a few words>

Status: working | done | failed - Branch: {{branch}} - Updated: <UTC time>

## Summary
Two to five sentences: what was asked, what you found, the answer.

## Results
Key numbers and outcomes, each with its evidence: the command that produced it and the log or
file it came from (path in git, or on the machine). Tables; plots committed under reports/.

## What was done
Numbered steps, with commit hashes.

## Decisions
Choices you made yourself, especially questions nobody answered: the options, your choice, why.

## Files left on the machine
Path, size and what it is (checkpoints, datasets, outputs in /root/outputs). Say which are needed
to reproduce the results.

## Caveats and next steps
Limits of the results, what to try next, what the user should check.

## Log
- <UTC time> <short progress note>   (newest last)
```

### 6.5 The worker's Claude Code settings (`/root/.claude/settings.json`)

```json
{
  "env": {"DISABLE_AUTOUPDATER": "1", "IS_SANDBOX": "1"},
  "permissions": {
    "deny": ["Bash(git push --no-verify:*)", "Bash(git commit --no-verify:*)", "Bash(git commit -n:*)",
             "Bash(git config core.hooksPath:*)", "Bash(git -c core.hooksPath:*)",
             "Bash(git remote set-url:*)", "Bash(git remote add:*)", "Bash(git push --mirror:*)"]
  }
}
```

Deny rules still apply under `--dangerously-skip-permissions`. They stop the obvious ways around
the hooks, but not a determined workaround (section 9). There are no ask rules, because an ask rule
would block an unattended worker.

---

## 7. Errors, exit codes and terminal output

### 7.1 Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | ok | Also `launch --dry-run` whether or not approval would be needed; `stop` of a stopped worker |
| 1 | error | Not initialized; Vast or GitHub errors; no admin rights; rent outcome unknown; unreachable machine; `open` on a stopped worker |
| 2 | usage | Bad arguments; invalid or taken name; branch exists; not in a GitHub repo; `secret set` or `open` without a terminal |
| 3 | needs approval / refused by a limit / would lose work | Pending launch, start or extend; budget too small; time limit reached on `start`; delete refused |
| 4 | no machine fits | No offer meets the requirements; all offers taken or failed to boot |
| 5 | setup failed | Upload or setup failed (machine destroyed) |

`cli.main` catches `CrabError` and prints `error: <message>` to stderr. Anything else unexpected
prints `error: unexpected <ExceptionType>: <text> (please report this)` and exits 1, with the
traceback only when `CRAB_STUDENT_DEBUG=1`. Ctrl-C prints `interrupted` and exits 1. A launch that is
interrupted can always be continued with `start`.

### 7.2 Wording rules

- ASCII only, including worker messages, which are shown with non-ASCII characters replaced by `?`.
- Short lines. Say what happened, then what to do, with the exact command to run.
- Money is `$1.26` (and `$0.42/hr`), durations are `1h12m`, sizes are `5.1 GB`.
- Messages talk to the user ("you", "your repo"), with no jargon such as "phase" or "record".
- Progress goes to stderr and results go to stdout. `--json` puts exactly one JSON value on stdout.

### 7.3 Launch output

Progress (stderr):

```
Planning ablation-1
  machine  1x RTX 4090 24 GB, 16 cores, 64 GB RAM, Quebec, CA - $0.42/hr (offer 123456)
  backups  2 more offers, up to $0.49/hr
  budget   $2.52 (2 x the 3.0h estimate at $0.42/hr); stops after 6h00m
  repo     me/proj - agent/ablation-1 from 1a2b3c4 + uncommitted changes to 2 files
Renting offer 123456... instance 987654
GitHub: deploy key added, agent/ablation-1 created
Booting: loading - pulling image 43% (1m05s)
Booting: running - waiting for ssh (1m38s)
Setting up 1/5 environment
Setting up 2/5 repository
Setting up 3/5 worker settings
Setting up 4/5 Claude Code
Setting up 5/5 starting the worker
Ready after 2m31s.
```

The **result line** goes to stdout, and is also `result_line` in `--json`:

```
ablation-1: working on 1x RTX 4090 (Quebec, CA) at $0.42/hr - est. 3.0h ~ $1.26 - budget $2.52 (stops after 6h00m) - branch agent/ablation-1 - watch: crab-student open ablation-1
```

Needing approval (exit 3):

```
ablation-1: NOT started - needs your approval: budget $84.00 is over auto_approve_usd ($50.00) - would rent 1x H100 SXM (Utah, US) at $2.10/hr, est. 20.0h ~ $42.00 - approve: crab-student approve ablation-1 (or: crab-student decline ablation-1)
```

Dry run (exit 0):

```
ablation-1 (dry run): would rent 1x RTX 4090 (Quebec, CA) at $0.42/hr - est. 3.0h ~ $1.26 - budget $2.52 (stops after 6h00m) - no approval needed
```

A fallback in use:

```
Offer 123456 did not boot (failed to set up container networking): destroyed it, trying offer 123999 ($0.44/hr)
```

`launch --json`, `approve --json` and `start --json` all print the same shape:

```json
{"name": "ablation-1", "state": "working", "result_line": "ablation-1: working on ...",
 "warnings": ["not carried over: 1 untracked file (notes.txt). Commit it and launch again if the worker needs it."],
 "needs_approval": false, "instance_id": 987654, "machine": "1x RTX 4090 24 GB, ...", "dph": 0.42,
 "hours": 3.0, "est_usd": 1.26, "budget_usd": 2.52, "limit_hours": 6.0,
 "repo": "me/proj", "branch": "agent/ablation-1"}
```

### 7.4 `list`

```
NAME          STATE        MESSAGE                                  TIME   SPENT / BUDGET
ablation-1    working      epoch 3/10, val loss 0.41                1h12m  $0.51 / $2.52
refactor-db   needs you    Drop the legacy_users table? (3 rows)    0h40m  $0.08 / $1.00
data-prep     done         cleaned 1.2M rows; see REPORT.md         2h05m  $0.31 / $0.80
old-sweep     stopped      idle stop - sweep 4/6 finished           3h10m  $1.40 / $4.00
big-train     approval     waiting for approval: budget $84.00 ...  -      $0.00 / $84.00
```

Layout rules:

- NAME is as wide as the longest name.
- STATE is 12 wide.
- MESSAGE takes the rest of the terminal width (`shutil.get_terminal_size`, at least 20) and is cut
  with `...`.
- TIME is `fmt_hours(used_hours)`, or `-` when unknown.
- The last column is `~spent / budget`.

With no workers: `No workers yet. Ask Claude Code to run something with crab-student, or see
crab-student launch --help.` `list --json` prints a list of `View` objects as dicts, using the keys
of the dataclass in 2.1.

### 7.5 `status NAME`

```
ablation-1: working - epoch 3/10, val loss 0.41
  machine   1x RTX 4090 24 GB, 16 cores, 64 GB RAM, Quebec, CA - $0.42/hr (instance 987654, running)
  repo      me/proj, branch agent/ablation-1 (all pushed)
  time      1h12m used of 6h00m (4h48m left); last activity 2 min ago; idle stop after 60 min
  money     ~$0.51 spent of the $2.52 budget (storage is billed while stopped, not counted here)
  open      crab-student open ablation-1    (VS Code: --vscode)
  results   crab-student report ablation-1 | crab-student pull ablation-1
```

For other states the relevant line is replaced:

- `waiting for approval: ... - crab-student approve ablation-1`
- `launch interrupted - crab-student start ablation-1 continues it`
- `stopped (idle stop) - crab-student start ablation-1`

`status --json` prints the `View` plus `result_line`.

### 7.6 `init`

```
crab-student setup
  Vast        ok - you@example.com, credit $23.40
  GitHub      ok - logged in as you
  SSH key     created ~/.crab-student/ssh/id_ed25519 (and added "Include ~/.crab-student/ssh/config" to ~/.ssh/config)
  Claude      In another terminal run: claude setup-token
              Paste the token (sk-ant-oat01-...), it will not be shown:
              ok - stored in the Windows Credential Manager
  Claude Code skill and permission rules installed in ~/.claude
  Tools       ssh, git, gh ok; wt found (open --all works)
Ready. In Claude Code, ask for a task to run on a GPU, e.g. "run the ablation on a GPU with crab-student".
```

### 7.7 Error examples

```
error: crab-student is not set up yet (no Vast key). Run: crab-student init
error: "Ablation" is not a valid name: use 3-40 characters from a-z, 0-9 and "-" (not at the start or end).
error: a worker named ablation-1 already exists. Pick another name, or delete it first: crab-student delete ablation-1
error: branch agent/ablation-1 already exists on me/proj. Pick another name.
error: this folder is not in a git repo with a GitHub remote. Run from your repo, pass --repo PATH, or use --no-repo.
error: no machine fits: no offer has 2 GPUs with 80 GB each in europe. See what is available with crab-student offers --gpus 2 --gpu-ram 80, or allow more regions: crab-student config regions ""
error: budget $1.00 is too small: the cheapest fitting machine costs $0.42/hr, so 3.0h needs at least $1.26.
error: ablation-1 is stopped, so crab-student cannot check it for unsaved work. Start it (crab-student start ablation-1) and delete again, or delete anyway with --force.
error: could not reach ablation-1 over ssh (Connection timed out). Vast says it is running; try again in a minute, or see crab-student status ablation-1.
error: setup failed at step 4 (Claude Code): the Claude token was refused (Invalid bearer token): run crab-student init --claude-token, then crab-student start ablation-1. The machine was destroyed.
```

---

## 8. Test plan

### 8.1 Principles

- Tests check behaviour (records, calls made, files, exit codes, JSON fields), never exact wording
  or call order that does not matter.
- No network: sockets only to fakes on 127.0.0.1. No real Vast, GitHub, home directory, `~/.ssh`,
  `~/.claude` or keyring.
- An autouse fixture sets `HOME`, `USERPROFILE` and `XDG_CONFIG_HOME` to `tmp_path` and replaces the
  keyring with an in-memory backend. It also sets `VAST_URL` (to a fake that rejects everything
  unless a test starts one) and `CRAB_STUDENT_GITHUB_URL` (to a temporary folder of bare repos).
- Polling constants (`launch.POLL`, `BOOT_TIMEOUT`, `LABEL_WAIT`, `vast` backoff) are monkeypatched
  small. The machine side uses `CS_ROOT` (a temporary root) and `CS_TICK_SECONDS`.
- Unit tests run on Windows, macOS and Linux. Tests that need tmux, fcntl, sshd or Docker are marked
  and run on Linux CI only.

### 8.2 The fakes

**Fake Vast (`tests/fake_vast.py`)** is a threaded `http.server` that implements every endpoint in
`vast.py` with the documented shapes. It checks the Bearer key and records every request, so tests
can assert "exactly one rent call". Tests set its behaviour:

- `offers`: the list returned by the search. It applies simple `eq/gte/lte/in` filters, `order` and
  `limit`, and supports `ask_contract_id` lookups. Tests can also make it ignore a filter, to check
  the client-side recheck.
- Rent outcome per offer:

  | Outcome | Behaviour |
  |---|---|
  | `ok` | Creates the instance |
  | `taken` | 400 `{"success": false, "error": "no_such_ask"}` |
  | `timeout_created` | Creates it, then answers only after the client's timeout |
  | `error_created` | Creates it, then answers 500 |
  | `error_none` | 500 and creates nothing |

- Boot script per instance: `boots_after=N` polls (the machine address is a ssh stub, or a real
  container in the e2e), `never` (stays loading), `boot_error` (status_msg `Error: failed to set up
  container networking`), or `exits`.
- `GET /api/v1/instances/` pages of 2, with `next_token`/`after_token`, so pagination is exercised.
- Two backends behind the same HTTP layer: `MemoryBackend` for unit tests, and `DockerBackend` for
  the e2e. `DockerBackend` does the following:
  - rent runs `docker run -d -p 0:22` with the env (`CONTAINER_ID`, `CONTAINER_API_KEY`,
    `CS_VAST_URL` pointing at the fake through `host.docker.internal`, `CS_TICK_SECONDS`, and the
    onstart in `CS_ONSTART`) and `-v <github dir>:<same path>`;
  - stop, start and destroy map to `docker stop`, `docker start` and `docker rm -f`;
  - add ssh key does a `docker exec` that appends to `/root/.ssh/authorized_keys`;
  - status comes from `docker inspect`.

  A stop call from inside the container (with `CONTAINER_API_KEY`) works just like the real one.

**Fake GitHub.** "GitHub" is a folder of bare repos, `<tmp>/github/<owner>/<repo>.git`.
`CRAB_STUDENT_GITHUB_URL` points at it, so `git_url(slug)` is a local path and every clone, push and
fetch is real git. The test's user repo keeps a real-looking `git@github.com:me/proj.git` remote plus
`url.<tmp>/github/.insteadOf git@github.com:`, so parsing and fetching are both exercised.
`tests/fake_gh.py` implements the `gh` calls we make:

- `auth status` and `api user`;
- `api repos/<slug>`, with `permissions.admin` set by the test;
- `user/repos` (create, which also makes the bare repo with a README commit);
- `repos/<slug>/keys` (POST and DELETE, stored in a JSON state file);
- `git/matching-refs`, `git/refs` (POST), `git/refs/heads/...` (DELETE) and `commits/HEAD`, which
  run git against the bare repo;
- `contents/REPORT.md?ref=` with the raw Accept header;
- `repo clone`.

Unit tests monkeypatch `github.gh` to call it in-process. The e2e puts a `gh` shim script first on
`PATH`.

**Fake machine for local unit tests.** A fixture replaces `machine.reachable`, `machine.remote` and
`machine.upload_and_setup` with a scripted `FakeMachines`. It records uploads (including the
generated file names and modes, so tests can assert that secrets are 0600 and never in argv) and
returns canned status, save and check JSON, or failures. The real ssh path is covered by the e2e.

**Fake Claude.** `remote/fake_claude.py` ships in the package and is used by `--fake-claude`, the
machine-side tests and the e2e (8.3).

### 8.3 The fake Claude's script

- **New session** (`--session-id S "<prompt>"`):
  1. Create the transcript `~/.claude/projects/<slug>/S.jsonl` and append a JSON line for each step.
  2. `crab-status working "fake: starting"`.
  3. Write `result.txt`, then `git add`, `commit` and `push`.
  4. Write a small `/root/outputs/fake-output.bin`, then write `REPORT.md` from the template in the
     worker rules, then `commit` and `push`.
  5. `crab-status done "fake run finished"`. It also tries `done` once *before* pushing, to check the
     refusal: it expects exit 1 and records it.
- **Resume** (`--resume S [note]`): appends `{"type": "resumed", ...}`.
- **Always:** it then reads stdin lines forever, appending each as a user line and printing
  `fake-claude: got "<line>"`.
- **`FAKE_CLAUDE_SLEEP`**: the seconds between steps (default 1).
- **Exit 1** when `--resume` names a missing transcript, like the real CLI.

### 8.4 Unit tests (behaviour to cover)

- **`test_offers.py`**:
  - each filter, and GB as 1000 MB;
  - CUDA taken from the image tag;
  - `end_date`, the model substring match, and regions (codes, continents, unknown geolocation
    excluded);
  - CPU picks the cheapest;
  - GPU prefers clearly better dlperf/$ within 1.5x, but not a marginal one;
  - among similar prices, the faster download wins;
  - ties are deterministic;
  - fallbacks are on different machines and at most 1.25x the price;
  - the automatic budget formula and minimum hours;
  - an explicit budget filters; too small gives exit 3, no hardware match gives exit 4.
- **`test_launch.py`** (fake Vast + fake gh + FakeMachines):
  - the happy path: the record is `ready`, one rent call, and the label was saved before the rent;
    the branch is at `start_sha`; the deploy key is created; secrets are in the upload's 0600 files
    and in no subprocess argv;
  - the gate: exit 3 with nothing rented and no GitHub writes; the max-machines gate; `approve`
    performs the launch with the approved budget as a cap; `decline` removes it;
  - a taken offer moves to the next;
  - a boot error or boot timeout destroys the instance and uses a fallback; all failing gives exit 4
    with phase `failed`;
  - `timeout_created` adopts by label with exactly one rent call; `error_none` gives exit 1, one rent
    call, and no other offer tried;
  - `start` later finds a late label, or reports it still missing;
  - an interruption during booting or setup (the test raises in the middle) is continued by `start`;
  - a setup failure destroys the machine, exits 5, and `start` retries it;
  - a second `bring_up` waits on the lock and returns the first one's result;
  - `--dry-run` makes no PUT, POST or DELETE anywhere;
  - an invalid name, a taken name or an existing branch gives exit 2; no admin rights gives exit 1;
  - a CPU task on a GPU host sends `NVIDIA_DISABLE_REQUIRE`;
  - `extend` raises the limit (running: `set-limit` sent; stopped: `limit_pushed` false, then sent at
    `start`); `start` refuses when the time limit is reached.
- **`test_workers.py`**:
  - every row of the display-state table (4.15);
  - ssh failure shows `unreachable`;
  - `stop` saves before the Vast stop, and still stops when the save fails;
  - `delete`:
    - a pending worker is removed with no Vast calls;
    - a stopped worker without `--force` gives exit 3 and no destroy;
    - reported problems give exit 3;
    - a clean check destroys the machine, removes the key, and keeps the branch unless it is at
      `start_sha`;
    - `--force` skips the check;
    - a `renting` record is destroyed if its label is found;
  - `watch` raises one alert per transition and none on the first refresh;
  - `send` passes the text on stdin.
- **`test_machine_side.py`** (cs.py with `CS_ROOT`; a fake `tmux` script on Linux):
  - `tick`:
    - idle after `idle_minutes` of quiet ticks;
    - each of the transcript, CPU and GPU resets the idle clock, and an attached but unused session
      does not;
    - the nudge comes once, at `idle/2`, and only for `needs-you`;
    - the warning comes once and re-arms after an extend;
    - the time-limit stop is held off during the grace;
    - `dt` is capped, and `used_seconds` survives a restart;
  - `save`:
    - tracked changes and small untracked files are committed and pushed to the bare repo, and big
      untracked files are skipped;
    - nothing is committed during a merge or on another branch;
  - `declare done` is refused when the tree is dirty, when commits are unpushed or when there is no
    REPORT.md, and accepted when clean;
  - `pre-commit` refuses a secret's value (and the message does not contain it) and a file over 50 MB;
  - `pre-push` refuses other refs and deleting the branch;
  - `check` reports outputs and large ignored files;
  - `status` has the documented keys;
  - `notify` sends the right payload per URL type (local HTTP sink);
  - `worker_env` strips `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and `CONTAINER_*`, and adds the token
    and secrets;
  - `cmd_claude` builds the new, resume and restart-note argv, and falls back to a new session id
    when the resume fails;
  - `setup` is idempotent (no second clone), and the `.claude.json` merge keeps other keys.
- **`test_github.py`**:
  - `local_repo` accepts the three URL forms;
  - `snapshot` leaves the index, worktree, branches and stash byte-for-byte unchanged, includes
    tracked changes and excludes untracked files;
  - `create_branch` pushes by URL;
  - `pull` fast-forwards, leaves a checked-out or diverged branch alone, and still updates the
    remote-tracking ref;
  - `delete_branch_if_at` only deletes at the given sha.
- **`test_core.py`**: config parse, validate and reset; `regions` validation; atomic saves; a lock
  held by another process (a subprocess) is refused; the keyring and the file fallback (0600 on
  POSIX); the name rule.
- **`test_vast.py`**: the auth header; GETs are retried on 5xx but a rent never is (call count);
  DELETE 404 is ok; pagination; the `VAST_URL` override.
- **`test_cli.py`**: exit code mapping; `--json` prints exactly one JSON value on stdout while
  progress goes to stderr; errors on stderr start with `error:`; all output is ASCII; `secret set`
  without a TTY gives exit 2; `guide launch` prints the guide; `config` set, get and reset.
- **`test_setup.py`**:
  - a second `init` asks nothing (input is patched to fail);
  - the settings merge keeps user entries and adds none twice;
  - the Include line goes at the top, once;
  - the skill is written;
  - the permission invariant: every command that spends beyond the gate, destroys or changes settings
    is in `ASK` and in no `ALLOW` pattern.

### 8.5 The end-to-end test (`tests/e2e/test_e2e.py`, Linux CI with Docker)

Setup:

- Build `tests/e2e/Dockerfile`: `ubuntu:24.04` with `openssh-server git tmux python3 curl`. Its
  entrypoint generates host keys, starts `sshd`, runs `bash -c "$CS_ONSTART"` like Vast would, then
  sleeps.
- Start the fake Vast with `DockerBackend` and the fake `gh` shim.
- Seed the Vast key file and a dummy Claude token in the (file-fallback) keyring.
- Run the real `crab-student init`. It creates the SSH key and ssh config, and installs the skill into
  the temporary `~/.claude`.

Scenario (real CLI, real ssh, real tmux, real `cs.py`, fake Claude):

1. From a user repo with one uncommitted tracked change and one untracked file, `launch e2e-one
   --hours 1 --fake-claude --json` (brief on stdin) exits 0. `result_line` is present; a warning
   names the untracked file; the bare repo's `agent/e2e-one` contains the snapshot commit.
2. Poll `status e2e-one --json` until the state is `done`. `report` prints the REPORT.md from the bare
   repo. `pull` creates `agent/e2e-one` locally, and the user's worktree and index are unchanged.
3. `send e2e-one` with `hello` makes the transcript contain `hello` (checked over ssh).
4. `stop` saves and stops the container, and `list --json` shows `stopped`. `start` brings it back,
   the transcript contains `resumed`, and tmux session `worker` exists.
5. `extend e2e-one 1` updates `limit_hours` in the machine's `config.json`.
6. Idle stop: `config idle_minutes 1`, launch `e2e-idle` (the fake Vast gives `CS_TICK_SECONDS=2`), and
   make a tracked file dirty over ssh. Within about 90 s the container stops itself through the fake
   API. The branch has the `crab-student: autosave (idle)` commit, and a local HTTP sink set as
   `notify_url` received a generic JSON `stopped` event.
7. Dead-man switch: start `e2e-idle` again and kill its watchdog with `docker exec ... pkill -f "cs.py
   watch"`, running no `list` or `status` meanwhile, because they would restart it (4.8). Back-date
   `booted` and `heartbeat` by 46 minutes with `touch -d`. Within about 70 s the fake Vast receives
   the stop call and the container stops.
8. Rent recovery: the fake answers the next rent with `error_created`. `launch e2e-two --fake-claude`
   still succeeds by label, and exactly one container exists for it.
9. `delete e2e-one` refuses with exit 3 (the fake left a file in `/root/outputs`), and `delete
   e2e-one --force` removes the container, the deploy key (fake gh state) and the record.
   `delete e2e-two --force` and `delete e2e-idle --force` clean up.

### 8.6 CI, lint and docs

- GitHub Actions runs unit tests on windows-latest, macos-latest and ubuntu-latest (Python 3.11 and
  3.13), `ruff check` (line length 110), and the e2e job on ubuntu-latest. The e2e is marked `e2e`
  and skipped when Docker is missing.
- A test counts non-blank, non-comment lines of `src/**/*.py` and fails above 2,700, which keeps the
  size target visible.
- Docs:
  - `README.md` for users: install (`uv tool install crab-student`), `init`, how to ask Claude Code,
    the commands, money and safety, what gets carried over from the repo, and branch protection
    advice.
  - `CLAUDE.md` for future sessions: the layout, how to run the tests, the invariants of section 9
    and the conventions, and a "first real run" checklist (cheapest offer, `--fake-claude`, a private
    test repo, delete afterwards).
  - `SPEC.md`: the record and machine file formats, the ssh commands, the onstart and dead-man
    contract, the stop rules, and the safety invariants.

---

## 9. Risks and uncertainties

| # | Risk | How the design contains it |
|---|---|---|
| 1 | **Vast API details** differ from what is documented or assumed: rent error shapes, whether `label` is returned exactly, pagination fields, whether `CONTAINER_API_KEY` may stop its own instance, onstart length or newline handling | All calls live in `vast.py` (100 lines), which parses tolerantly (`.get`, several error keys). The onstart is short (about 900 characters) and every line is a whole command, so it can be joined with `; `. The first real run follows the checklist in CLAUDE.md with `--fake-claude` on the cheapest offer. If the scoped key cannot stop, the dead-man and watchdog stop calls fail visibly in their logs, and `list` still shows the machine running, so the user sees it |
| 2 | **A deploy key can push to any branch** of its repo, and hooks run on the client, where a root worker could bypass them | Several layers: the pre-push hook, the push refspec, deny rules for the obvious bypasses, and the worker's rules. The key is per repo, per worker, and removed on delete. The README recommends branch protection on `main` (deploy keys obey it). This is stated honestly in SPEC.md as "prevents accidents, not a hostile worker" |
| 3 | **Claude Code changes**: a new first-run dialog, different resume behaviour, a different transcript location, or flag changes | Known dialogs are pre-accepted. Setup step 5 waits for the conversation file and, if it does not appear, fails with the pane's text, so the cause is visible at once. Activity uses a glob over `~/.claude/projects/*/*.jsonl`, whatever the folder naming. A failed resume falls back to a new session that reads REPORT.md. The token is checked at setup |
| 4 | **Idle detection is wrong**. It can stay busy: a runaway loop, or a hung job holding the CPU. It can go idle: a network-bound job below 0.25 core | The time limit bounds any false "busy". Against false "idle", the worker checks long jobs at least every 9 minutes, and each check grows the conversation. CPU comes from the container's cgroup, not the host. Without cgroup files, CPU is ignored; the conversation and GPU signals still work, and the time limit still holds |
| 5 | **The background launcher** cannot answer prompts: a permission rule may not match a heredoc command line, and the Bash tool stops at 10 minutes | Every command the launcher needs is allowed. The brief goes on stdin, so there is no file write. Launches continue with `start`. If heredoc matching fails in practice, the skill switches to the main session running `launch` with `run_in_background` (a skill-text change, no code) |
| 6 | **Windows**: OpenSSH key ACLs, `wt` argument parsing, VT escape codes, Git Bash path rewriting, keyring backends | Keys only come from ssh-keygen. `wt` and ssh get plain arguments (`crab-attach` avoids nested quoting). `os.system("")` turns on VT. No CLI argument starts with `/`. The keyring falls back to a 0600 file |
| 7 | **An unknown rent outcome** is never found by its label | The launch stops, never rents again, and tells the user exactly how to look (`start`) or give up (`delete`, plus where to check on Vast) |
| 8 | **Money leaks**: stopped machines bill storage; an interrupted launch before the upload leaves a machine until the dead-man stops it (45 min), and then it bills storage | `list` shows every recorded machine, including `interrupted` and `stopped`. `status` mentions storage billing. The README tells users to delete finished workers. Nothing is ever rented outside the records |
| 9 | **The worker is root** and can read `vast.env`, the deploy key and the secrets | This is accepted on a disposable single-task machine. The instance key is scoped to that one machine. `CONTAINER_*` is removed from the worker's environment, and its rules forbid Vast calls and printing secrets. Transcripts never leave the machine, and the pre-commit hook stops secret values from reaching git |
| 10 | **Time accounting** leaves out boot time and storage | The budget factor of 2 gives margin. `status` says the figure is approximate |
| 11 | **GitHub host key pinning** goes stale | The ed25519 key (`AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl`) is written by init. Check it against `https://api.github.com/meta` when implementing. A mismatch fails clearly ("host key verification failed") at the branch push, before anything on the machine |
| 12 | **Partial clones or LFS** | If `git clone --filter=blob:none` fails, it is retried once without the filter (2 lines). LFS files arrive as pointers; the worker can run `git lfs pull` (the README notes this) |
| 13 | **Line budget creep** | Per-file budgets (2.3) and a line-count test (8.6). Formatting lives only in `cli.py`; there are no per-command classes, no plugin points and no options beyond the requirements |
