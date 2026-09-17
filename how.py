#!/usr/bin/env python3
"""how — per-project shell command memory.

Learns the commands you actually run in each directory, then shows them
ranked by frequency+recency when you come back. Never touches your shell's
own history, keybindings, or prompt: it's a plain command you run when you
want answers.

    # 1) record (from bash/zsh PROMPT_COMMAND / precmd hook — see README):
    how record -- $COMMAND

    # 2) ask, standing anywhere inside a project:
    how                      # top commands for this directory
    how -n 20                # more of them
    how grep                 # only commands containing 'grep'
    how --json               # machine-readable

Storage: JSON at ~/.local/share/how/how.json (XDG-aware, respects $HOW_DB).
Noise like `ls`, `cd`, `git status` is skipped on record by default.

Exit codes: 0 ok (even with empty output), 1 usage/IO error, 2 dirty flags
(unknown command, bad -n, unwritable store).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
from pathlib import Path

__version__ = "0.1.0"

PROG = "how"

# ----------------------------------------------------------------------------
# storage

DEFAULT_DB_HINT = "$HOW_DB or ~/.local/share/how/how.json (XDG-aware)"

# weight = freq^ALPHA * exp(-age_days / TAU): recent habits dominate,
# but years-old muscle memory still surfaces
ALPHA = 0.6
TAU_DAYS = 21.0


def store_path(db: Path | None) -> Path:
    """Resolve the store path. $HOW_DB is read at call time (not import
    time) so tests and hooks can retarget it per-invocation."""
    if db is not None:
        return db
    env = os.environ.get("HOW_DB")
    if env:
        return Path(env)
    return Path(
        os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            PROG,
            "how.json",
        )
    )


def load_store(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        die(f"cannot read store {path}: {exc}")
    if not isinstance(data, dict):
        die(f"store {path} is not a JSON object")
    return data


def save_store(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        die(f"cannot write store {path}: {exc}")


# ----------------------------------------------------------------------------
# recording

# noise we never keep: zero-information commands (seen one, seen them all)
NOISE_RE = re.compile(
    r"""^(?:
      h|history|help|clear|exit|q$|qu$|c$|l$|ll$|la$|ls.*|-?cd.*|pwd
    | (?:git|g)\s+(?:status|st|diff|branch|log.*|show)
    | (?:g?co|g?sw)(?:\s.*)?$          # bare checkout/switch listings
    | (?:ga|gca|gst|gs)$
    | vi?m?$|nano$                     # bare editor, no file
    | man\s|which\s|type\s
    | (?:\.?source|\.)\s.*rc$          # shell rc reloads
    )$""",
    re.VERBOSE,
)

# commands with these binaries are one-shot/expiring: useful last week,
# noise today (build ids, one-off fixes, temp scripts)
EXPIRING_BIN = {
    "pip", "pip3", "npm", "yarn", "pnpm", "cargo", "gradle", "mvn",
    "apt", "apt-get", "sudo", "systemctl", "docker", "kubectl",
}
EXPIRING_RE = re.compile(r"^(sudo\s+)?(" + "|".join(EXPIRING_BIN) + r")(\s|$)")

MAX_LEN = 400  # commands longer than this are scripts, not habits


def is_noise(cmd: str) -> bool:
    c = cmd.strip()
    if not c or len(c) > MAX_LEN or "\n" in c:
        return True
    if NOISE_RE.match(c):
        return True
    return False


def is_expiring(cmd: str) -> bool:
    """PackageManager-ish: rank by recency only, not frequency."""
    return bool(EXPIRING_RE.match(cmd.strip()))


def cmd_root(cmd: str) -> str:
    """Key for dedup: the command itself, whitespace-normalized.

    Identical invocations count up; variants (different flags/args) are
    separate habits — last-wins merging of near-twins is worse than a
    few extra rows, and the recency weight fades one-offs anyway.
    """
    return " ".join(cmd.strip().split())


def record(store: dict, path: Path, cmd: str, now: float) -> bool:
    """Add one observation. Returns True if kept (not noise).

    On a store that cannot be written, exits 1 loudly: a silent hook that
    drops history is worse than a noisy one. (Noise filtering stays silent
    on purpose — that's the common path.)
    """
    if is_noise(cmd):
        return False
    dirkey = os.getcwd()
    entry = store.setdefault(dirkey, {}).setdefault(cmd_root(cmd), {})
    entry["cmd"] = cmd.strip()
    entry["n"] = int(entry.get("n", 0)) + 1
    entry["ts"] = now  # last-seen
    entry["x"] = 1 if is_expiring(cmd) else 0
    save_store(path, store)
    return True


# ----------------------------------------------------------------------------
# scoring / listing

def weight(n: int, ts: float, now: float, expiring: bool) -> float:
    age = max(0.0, (now - ts) / 86400.0)
    if expiring:
        return math.exp(-age / (TAU_DAYS / 3))  # fast fade
    return (n ** ALPHA) * math.exp(-age / TAU_DAYS)


def dir_candidates(store: dict, start: Path) -> dict:
    """Entries for this dir plus every recorded ancestor of it.

    Working in a subdir of a recorded project root should surface the
    project's commands: the hook records subdirs too, but ancestors carry
    the longer history.
    """
    out: dict = {}
    start_abs = str(start.resolve())
    for d in store:
        try:
            if start_abs == d or start_abs.startswith(d.rstrip("/") + "/"):
                out.update(store[d])
        except (AttributeError, ValueError):
            continue
    return out


def list_cmds(
    store: dict,
    start: Path,
    needle: str,
    limit: int,
    now: float,
    all_dirs: bool,
) -> list[dict]:
    if all_dirs:
        pool: dict = {}
        for d in store:
            pool.update(store[d])
    else:
        pool = dir_candidates(store, start)
    items = []
    for entry in pool.values():
        cmd = entry.get("cmd", "")
        if needle and needle not in cmd:
            continue
        items.append(
            {
                "cmd": cmd,
                "n": int(entry.get("n", 0)),
                "ts": float(entry.get("ts", 0)),
                "weight": weight(
                    int(entry.get("n", 0)),
                    float(entry.get("ts", 0)),
                    now,
                    bool(entry.get("x", 0)),
                ),
            }
        )
    items.sort(key=lambda it: it["weight"], reverse=True)
    return items[:limit]


# ----------------------------------------------------------------------------
# output

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def human_when(ts: float, now: float) -> str:
    if ts <= 0:
        return "never"
    days = (now - ts) / 86400.0
    if days < 1 / 24:
        return "just now"
    if days < 1:
        return f"{int(days * 24)}h ago"
    if days < 31:
        return f"{int(days)}d ago"
    if days < 365:
        return f"{int(days / 30)}mo ago"
    return f"{int(days / 365)}y ago"


def print_human(items: list[dict], now: float, color: bool, title: str) -> None:
    if not items:
        return
    def dim(s: str) -> str:
        return DIM + s + RESET if color else s
    def bold(s: str) -> str:
        return BOLD + s + RESET if color else s
    print(dim(f"  {title}"))
    for i, it in enumerate(items, 1):
        when = human_when(it["ts"], now)
        meta = f"{it['n']}x, {when}"
        num = str(i).rjust(2)
        print(f"{num} {bold(it['cmd'])} {dim('(' + meta + ')')}")


def print_json(items: list[dict], now: float, title: str) -> None:
    payload = {
        "generated": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(),
        "dir": os.getcwd(),
        "count": len(items),
        "commands": [
            {
                "cmd": it["cmd"],
                "count": it["n"],
                "last_ts": round(it["ts"], 3),
                "last_seen": human_when(it["ts"], now),
                "weight": round(it["weight"], 4),
            }
            for it in items
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1))


# ----------------------------------------------------------------------------
# cli

def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"{PROG}: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_record(args: argparse.Namespace, store: dict, path: Path, now: float) -> None:
    # Preferred input: $HOW_CMD env var (hook-friendly, no quoting hell).
    # Fallback: positional args after 'record'.
    raw = os.environ.get("HOW_CMD") or " ".join(args.cmd).strip()
    if not raw:
        die("nothing to record (set HOW_CMD or pass the command)", 2)
    record(store, path, raw, now)  # silent on success: runs after every prompt


def cmd_stats(args: argparse.Namespace, store: dict, path: Path, now: float) -> None:
    items = list_cmds(store, Path.cwd(), args.needle, args.limit, now, args.all)
    if args.json:
        print_json(items, now, "json")
        return
    title = "everywhere" if args.all else str(Path.cwd().resolve())
    scope = f" matching {args.needle!r}" if args.needle else ""
    print_human(items, now, use_color(args.color), f"{title}{scope}, top {len(items)}")
    if not items:
        where = "anywhere" if args.all else "here or in project ancestors"
        print(f"  nothing recorded {where} yet", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Per-project shell command memory: what do I usually run here?",
        epilog="""examples:
  how                       # top commands for this directory/project
  how -n 30                 # show more
  how docker                # only commands mentioning docker
  how --all grep            # search across all recorded projects
  how record -- ./bin/deploy.sh   # record one command by hand
  how init bash             # print shell hook lines
  how forget htop           # drop every entry matching a substring
  how --json | jq '.commands[0].cmd'

storage:
  ~/.local/share/how/how.json   (XDG-aware; override with $HOW_DB)""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", metavar="PATH", type=Path, default=None,
                   help=f"store file (default: {DEFAULT_DB_HINT})")
    p.add_argument("-n", "--limit", type=int, default=15, metavar="N",
                   help="show top N commands (default: 15)")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                   help="colorize output (default: auto)")
    p.add_argument("--json", action="store_true", help="JSON to stdout (for pipes)")
    p.add_argument("--all", action="store_true",
                   help="search across all recorded directories, not just this project")
    p.add_argument("needle", nargs="?", default=None,
                   help="substring filter on command text")
    # subcommands (record/dirs/forget/init) are hand-split in main() before
    # argparse; the parser below only ever sees global flags + needle.
    return p


# Copy-paste hook lines. bash needs ${COMMAND:?} (last command, pre-Enter);
# zsh hooks must call `how` inside the precmd() body.
HOOK_BASH = """PROMPT_COMMAND='HOW_CMD="$COMMAND" how record; '""" + '"${PROMPT_COMMAND:+$PROMPT_COMMAND}"'
HOOK_ZSH = 'precmd() { HOW_CMD="$COMMAND" how record; }'


def cmd_init(shell: str) -> None:
    if shell == "bash":
        print(HOOK_BASH)
    else:
        print(HOOK_ZSH)


def cmd_dirs(store: dict, args: argparse.Namespace) -> None:
    rows = []
    for d, entries in store.items():
        rows.append((sum(int(e.get("n", 0)) for e in entries.values()), d, len(entries)))
    rows.sort(reverse=True)
    color = use_color(args.color)
    for total, d, uniq in rows:
        dshow = CYAN + d + RESET if color else d
        print(f"{str(total).rjust(6)}  {dshow}{f'  ({uniq} cmds)' if uniq != total else ''}")


def cmd_forget(store: dict, path: Path, args: argparse.Namespace) -> None:
    pat = args.pattern
    removed = 0
    for d in list(store):
        ent = store[d]
        for key in list(ent):
            if pat in ent[key].get("cmd", ""):
                del ent[key]
                removed += 1
        if not ent:
            del store[d]
    save_store(path, store)
    print(f"forgot {removed} entr{'y' if removed == 1 else 'ies'} matching {pat!r}")


SUBCOMMANDS = {"record", "dirs", "forget", "init"}
# global flags that consume the next token (everything else is = style or boolean)
_FLAGS_WITH_VALUE = {"--db", "-n", "--color"}


def split_subcommand(raw: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Split argv into (subcommand, global_flags, rest).

    The subcommand, when present, is the first positional token; global
    flags may precede it (`how --db X record cmd`). Positional tokens that
    aren't subcommands (the needle) end the scan.
    """
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok.startswith("-") and tok != "-":
            i += 1 if ("=" in tok or tok not in _FLAGS_WITH_VALUE) else 2
            continue
        if tok in SUBCOMMANDS:
            return tok, raw[:i], raw[i + 1:]
        return None, raw, []
    return None, raw, []


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    sub_name, global_flags, rest = split_subcommand(raw)

    if sub_name == "record":
        if "--" in rest:
            rest = rest[rest.index("--") + 1:]  # `how record -- cmd`
        elif rest and rest[0].startswith("-") and rest[0] != "-":
            # flags we don't understand before the command: drop them
            # (primary record API is $HOW_CMD env anyway)
            rest = [t for t in rest if not t.startswith("-")]
    elif sub_name == "forget":
        # keep only the pattern words: strip flag pairs from `rest`
        cleaned, skip = [], False
        for tok in rest:
            if skip:
                skip = False
                continue
            if tok in _FLAGS_WITH_VALUE or tok == "--db":
                skip = True
                continue
            if tok.startswith("--db=") or tok.startswith("-"):
                continue
            cleaned.append(tok)
        rest = cleaned

    try:
        args = build_parser().parse_args(global_flags)
    except SystemExit as exc:
        return int(exc.code or 0)

    path = store_path(args.db)
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()

    if sub_name == "init":
        shell = rest[0] if rest and rest[0] in ("bash", "zsh") else "bash"
        cmd_init(shell)
        return 0

    store = load_store(path)

    if sub_name == "record":
        raw_cmd = os.environ.get("HOW_CMD") or " ".join(rest).strip()
        if not raw_cmd:
            die("nothing to record (set HOW_CMD or pass the command)", 2)
        record(store, path, raw_cmd, now)  # silent: runs after every prompt
        return 0

    if sub_name == "dirs":
        cmd_dirs(store, args)
        return 0

    if sub_name == "forget":
        if not rest:
            die("forget: pattern required", 2)
        cmd_forget(store, path, argparse.Namespace(pattern=" ".join(rest)))
        return 0

    if args.limit <= 0:
        die("limit must be positive", 2)
    # stats mode: needle is the first positional of global_flags argv —
    # split_subcommand returned it as a non-sub positional
    args.needle = _first_positional(global_flags) or args.needle
    cmd_stats(args, store, path, now)
    return 0


def _first_positional(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("-") and tok != "-":
            i += 1 if ("=" in tok or tok not in _FLAGS_WITH_VALUE) else 2
            continue
        return tok
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.close(1)  # `how | head` must not traceback
        sys.exit(0)
