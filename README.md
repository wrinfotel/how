# how

Your shell history, sliced per project, ranked by frequency, cleared of noise.

`how` remembers the commands you actually run in each directory, then shows
them ranked when you come back — so "what was that build command in this repo
again?" becomes one keystroke instead of a Ctrl+R archaeology dig.

```bash
$ cd ~/work/payment-service
$ how
  ~/work/payment-service, top 3
 1 ./gradlew bootRun --profiles dev    (34x, 3 days ago)
 2 docker compose up -d db             (28x, yesterday)
 3 ./gradlew test --tests '*Parser*'   (12x, last week)
```

- **Per-project memory.** Records every command together with its working
  directory. Standing in any subfolder, you see the project's habits, not
  the whole world.
- **Frequency + recency ranking.** `weight = n^0.6 · exp(-age/21d)`: fresh
  habits dominate, but old muscle memory still surfaces. Package-manager
  commands (`pip install`, `sudo apt`, `gradle`…) decay faster — they rot.
- **Noise filter.** `ls`, `cd`, `git status`, `history`, one-letter typos
  and friends never get recorded.
- **Zero dependencies.** One Python file, stdlib only.
- **Doesn't touch your shell.** No Ctrl+R hijack, no keybindings, no daemon.
  `how` is just a command you run.

## Install

```bash
mkdir -p ~/.local/bin
cp how.py ~/.local/bin/how && chmod +x ~/.local/bin/how
```

Then add the hook (one line, prints itself):

```bash
echo 'eval "$(how init bash)"' >> ~/.bashrc    # or zsh for ~/.zshrc
```

What it prints is literally:

```bash
__how_record() { local c; c="$(HISTTIMEFORMAT= history 1 | sed -E 's/^[[:space:]]*[0-9]+[[:space:]]+//')"; [ -n "$c" ] && HOW_CMD="$c" how record; }; PROMPT_COMMAND="__how_record; ${PROMPT_COMMAND:+$PROMPT_COMMAND}"
```

(The hook resolves the last command from bash history itself — bash has no
`$COMMAND` variable, so a hook that references it records nothing.)

The record call is silent and fast (JSON rewrite, no SQLite, no daemon).
To see how it looks without installing anything:

```bash
python3 how.py init zsh
```

## Usage

```
how                    # top commands for this directory (+ ancestors)
how -n 20              # more of them
how docker             # only commands containing 'docker'
how --all              # search across ALL known projects
how --json             # machine-readable (jq-friendly)
how record -- make x   # record one command by hand
how forget htop        # drop entries matching a substring
how dirs               # all known projects, by usage
```

Filtering out noise is on by default; nothing else happens on record.

Exit codes: `0` ok (even when nothing matches), `1` store I/O error
(unreadable/corrupt/unwritable — loud on purpose, silent hooks that drop
history are worse), `2` usage errors.

## Storage

JSON at `~/.local/share/how/how.json` (XDG-aware). Override with `--db` or
`$HOW_DB`. Want the team to share the project's commands? Export and
commit:

```bash
how --json > .how.json    # snapshot for the repo, teammates can diff
```

## Why not Atuin / McFly?

Both are great, but both capture your whole shell (Ctrl+R, up-arrow) and
focus on search. `how` is the opposite bet: a small showcase of *this
project's* habits that never touches your shell. If you're happy with
Atuin, stay with it.

## Development

```bash
python3 -m unittest discover tests   # 28 tests, stdlib only
```

MIT license.
