# Agent supervisor for Claude Code

A standing **supervisor** session per machine that you talk to instead of juggling a dozen AI coding sessions. It keeps a live, zero-token view of every Claude Code, Codex and OpenCode session on your Happier account, hands work to **worker** sessions with written briefs and pass/fail criteria, answers the questions it is allowed to answer, verifies "done" against evidence, archives finished workers, and interrupts you only for what genuinely needs you. A **hypervisor** session audits and improves the supervisors themselves, and a **catchup** skill tells you what changed since you last looked.

Three skills, three hooks, two Python scripts, and their tests:

```
skills/supervisor/SKILL.md        the supervisor's operating rules
skills/supervisor/profiles/       one file per machine (example.md is the template)
skills/supervisor/scripts/ledger.py   append-only work ledger + published summary
skills/supervisor/scripts/fleet.py    session inventory, zero-token watcher, safe messaging,
                                      archive/park/reopen, catch-up, self-compaction
skills/hypervisor/SKILL.md        audits the supervisors and owns the supervisor skill
skills/catchup/SKILL.md           "what did I miss" for a supervisor or the hypervisor
hooks/supervisor-event.sh         Notification + Stop: append each session's events to the log
hooks/supervisor-plan-guard.sh    UserPromptSubmit: undo a plan mode a Happier resume caused
hooks/guard-watch-untracked.sh    PreToolUse(Bash): block starting the watcher detached
hooks/tests/                      shell + Python regression suites
config/settings.snippet.json      the hook wiring to merge into ~/.claude/settings.json
config/gitignore.snippet          for a git-synced ~/.claude only
ROADMAP.md                        how to build an equivalent system from scratch
```

## Architecture

```
 you (Happier app: phone / desktop / web)
   |  type to one session per machine
   v
 Supervisor (Claude Code session in ~/supervisor) -----fleet.py send----> peer supervisor on another machine
   |  ledger.py add / set / decide        ^                                 (same Happier account)
   |  happier session create              | wakes when its background job exits
   v                                      |
 Workers (Claude / Codex / OpenCode)    fleet.py watch  (harness-tracked background job, costs no tokens)
   |  Notification / Stop hooks             ^        ^
   v                                        |        |
 ~/.claude/state/supervisor/events.jsonl ---+   backstop poll: claude agents --json + happier session list
```

- **The ledger is the memory, not the conversation.** `ledger.jsonl` is append-only and local; `ledger.md` and `~/.claude/supervisors/<profile>.md` (a whitelisted, truncated, credential-redacted summary) are re-rendered on every write. A restart or compaction re-reads them, so it is a non-event.
- **Waiting costs nothing.** The watcher is a background Bash job whose exit is the wake. Hooks append events; a 60 s backstop poll catches what hooks miss. It wakes only for something new or stuck (owned worker finished a turn, a permission prompt, a worker busy too long, idle with no `STATUS:` line, gone). When the fleet is quiet it hands off to a detached "doorbell" that rings the supervisor with one Happier message only when something happens.
- **Content yes, permission never.** The supervisor answers questions the code and docs can settle; it never approves a permission prompt or an outbound message. Questions for you must be one of four kinds (outbound, security, taste, action) and say what was already tried.
- **Multi-machine** (optional): one supervisor per machine, each owning its own workers; cross-machine work is a message to the other machine's supervisor. With `~/.claude` synced through a private git repo, each machine's published summary is readable by the others and by the hypervisor.

## Requirements

- **Claude Code**, recent enough to have `claude agents --json`.
- **Happier** (https://github.com/happier-dev/happier), the open-source relay and apps for AI coding sessions: CLI installed, signed in, and daemon running on every machine that hosts a supervisor or workers, all on ONE account. Behaviour notes in the supervisor skill were measured on Happier 0.2.16.
- **python3** 3.9+ (standard library only) and POSIX `sh`. Linux is the tested platform; macOS works with a fallback (no `/proc`, so sessions are matched by directory). Windows is not supported (`fcntl`).
- **git**, only for a multi-machine setup and for one test.
- Optional: `gh` (verifying PRs and CI), `plocate`, `jq` (the guard hook uses it when present, else python3), Codex and OpenCode as extra backends.

## Install

```sh
PKG=/path/to/agent-supervisor
mkdir -p ~/.claude/skills ~/.claude/hooks/tests
cp -R "$PKG/skills/supervisor" "$PKG/skills/hypervisor" "$PKG/skills/catchup" ~/.claude/skills/
cp "$PKG"/hooks/*.sh ~/.claude/hooks/ && chmod +x ~/.claude/hooks/supervisor-*.sh ~/.claude/hooks/guard-watch-untracked.sh
cp "$PKG"/hooks/tests/* ~/.claude/hooks/tests/
```

Keep the directory names. The hooks, `ledger.py` and all three skills refer to `~/.claude/skills/supervisor/` and each other by path and name, so a renamed copy breaks quietly.

**Wire the hooks.** Merge `config/settings.snippet.json` into `~/.claude/settings.json`: append each entry to the existing array for that event rather than replacing it. The two supervisor hooks are silent no-ops on any machine without `~/.claude/state/supervisor/`. The guard hook runs everywhere, but it only blocks a Bash command that starts `fleet.py watch` detached, so wiring all three everywhere is safe.

**Per machine:**

1. `cp ~/.claude/skills/supervisor/profiles/example.md ~/.claude/skills/supervisor/profiles/<name>.md` and fill it in (host, worker permission mode, what this machine owns, routing to other machines, search, traps).
2. `python3 -I ~/.claude/skills/supervisor/scripts/ledger.py init --profile <name>` (creates the state dir, which turns the hooks on, and the `~/supervisor` launcher).
3. Start the supervisor as a Happier session in `~/supervisor`, following `<setup>` in `skills/supervisor/SKILL.md`, and talk to it from the Happier app.

**Hypervisor** (optional, one machine): create `~/hypervisor/CLAUDE.md` as described in `skills/hypervisor/SKILL.md` and start a Happier session there.

**Multi-machine** (optional): keep `~/.claude` in a PRIVATE git repo (`<YOUR_CONFIG_REPO>`) on each machine, and add `config/gitignore.snippet` to its `.gitignore` so the ledger and event log never leave the machine. You need your own auto-sync that commits and pushes `~/.claude` (for example Claude Code hooks or a timer running `git pull --ff-only` and `git push`); it is not included. The "config sync stuck" wake and `catchup --fleet` assume it pushes to `origin/main` and marks its commits `[auto] sync`; change `SYNC_BRANCH` and `AUTO_SYNC_MARK` at the top of the sync section in `fleet.py` if yours differ. Without a synced repo, these features stay quiet and everything else works on one machine.

Skills load in the next Claude Code session, not the current one; confirm with `/help`.

## Run the tests

From the package (before installing) or from `~/.claude` (after). Each suite makes its own throwaway HOME, so no real state is touched:

```sh
cd /path/to/agent-supervisor        # or: cd ~/.claude
sh hooks/tests/supervisor-event.test.sh       # hook matrix + supervisor_test.py
sh hooks/tests/guard-watch-untracked.test.sh  # guard hook matrix
```

From `~/.claude`, the one test of `config/gitignore.snippet` is skipped, since that file is not installed.

Each test file tests the tree it sits in (two directories up), never `~/.claude` by assumption. Nothing calls the real `happier` or `claude`: they are stubbed. Expect about 30 s for the Python suite.

## License

MIT. See `LICENSE`.
