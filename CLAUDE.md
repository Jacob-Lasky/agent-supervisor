# Setting this up for your user

You are an AI coding agent, and your user has pointed you at this repo so you can install it on their machine. This file is your runbook. The README is the human overview; read it once for the architecture, then follow the steps below in order. Report what you did after each step in one line.

What gets installed: a standing **supervisor** session on this machine that the user talks to instead of juggling many coding sessions. It hands work to worker sessions, answers their questions, checks their claims, and interrupts the user only when it must. Optionally, a **hypervisor** session on one machine keeps several machines' supervisors in step. Everything runs on [Happier](https://github.com/happier-dev/happier), which lists and messages Claude Code, Codex and OpenCode sessions across machines and on the user's phone.

## Ground rules

- **Do not edit `~/.claude/settings.json` by hand.** `install.py` merges the hook wiring, keeps every existing setting and hook, and backs up anything it replaces to `~/.claude/backups/agent-supervisor-<timestamp>/`. Run it with `--dry-run` first and show the user the plan.
- **Recommend the worker permission mode; the user confirms it** (step 4). `yolo` lets workers act without permission prompts, so it is only safe when the user already has PreToolUse hooks that block outbound messages and pushes to shared repos. `auto` is the safe default.
- **Stop and hand back to the user** for anything only they can do: installing or signing in to Happier, approving a permission prompt, or a choice in step 4. Say exactly what they need to do, then wait.
- **Do not start project work or workers yourself.** Your job ends when the supervisor session is running and verified. The supervisor starts workers.

## Steps

### 1. Check prerequisites

Run each check, and stop with a clear message if one fails:

- `claude --version`, and `claude agents --json` prints a JSON list. If `agents` is missing, Claude Code is too old: the user should update it.
- `python3 --version` is 3.9 or newer.
- `happier --version`, then `happier session list --json` returns `"ok": true` (signed in), and `happier daemon status --json` shows `"running": true` (this machine's daemon is up, so sessions can start here). If `happier` is missing, not signed in, or its daemon is down, stop. The user installs it, signs in, and starts the daemon on this machine, following https://github.com/happier-dev/happier. Every machine must use the same Happier account.
- `git --version` (optional: needed only for the multi-machine setup and one test).

### 2. Run the test suites from the repo

```sh
export PYTHONDONTWRITEBYTECODE=1               # keep the repo clean
sh hooks/tests/supervisor-event.test.sh        # hook matrix + the Python suite (about 30 s)
sh hooks/tests/guard-watch-untracked.test.sh   # guard hook matrix
python3 -I tests/install_test.py               # the installer
```

All three must pass. The suites stub `happier` and `claude` and use a throwaway HOME, so they touch nothing outside it. A failure here means this machine differs from what the code expects; report the failing test name and output to the user rather than working around it.

### 3. Install

```sh
python3 -I install.py --dry-run    # show the user this list
python3 -I install.py
```

The user asked for the install, so show the dry-run list and go ahead; stop only if they object. Keep the list of `add` lines from the real run: Undo needs it. Run it a second time: it must print `already installed: nothing to change`. If `~/.claude/settings.json` is not valid JSON, the installer refuses. Tell the user, and do not repair the file without their yes.

### 4. Write this machine's profile

First, look at what `~/.claude/settings.json` already has under `hooks.PreToolUse`: is anything there that blocks messages to other people (Slack, email, issue and PR comments) and pushes to shared repos? Then ask the user these questions, in one message:

1. **A short name for this machine**, for example `laptop` or `devbox`. Use lowercase letters, digits, `-` and `_`, and suggest one based on `hostname`.
2. **What work belongs on this machine**: which repos, services and kinds of task. If they will run more than one machine, also ask what goes to each of the others.
3. **The worker permission mode**: recommend `yolo` only if you found such guard hooks, otherwise `auto`, and say which hooks you found (or that you found none).

Then copy `~/.claude/skills/supervisor/profiles/example.md` to `~/.claude/skills/supervisor/profiles/<name>.md` (the installed copy, not this repo) and fill in every field. Delete the **Routes to** line if this is the user's only machine. Work out the rest yourself rather than asking:
- **Host match**: what `hostname` prints.
- **Home / launcher**: `$HOME/supervisor`.
- **Search**: whether `locate` is installed (`command -v locate`) and where its database is.
- **Traps**: anything you noticed while doing steps 1 to 3, such as a PATH entry that non-interactive shells lack.

Delete the optional heavy-build section unless the user says several workers will compile on this machine at once. Show the user the finished profile.

### 5. Initialise the machine

```sh
python3 -I ~/.claude/skills/supervisor/scripts/ledger.py init --profile <name>
python3 -I ~/.claude/skills/supervisor/scripts/ledger.py render
```

`init` creates `~/.claude/state/supervisor/`, which switches the hooks on for this machine. It also creates the launcher `~/supervisor/CLAUDE.md` and `~/supervisor/AGENTS.md`, and it refuses the unfilled `example` profile.

### 6. Start the supervisor

First run `python3 -I ~/.claude/skills/supervisor/scripts/fleet.py snapshot`. If it already lists a session flagged `SUPERVISOR` under this machine, stop and ask the user: two supervisors on one machine fight over the same workers.

The supervisor is a Happier session in `~/supervisor`. Start it in three commands, so the permission mode is set even if your own session is in plan mode:

```sh
happier session create --path ~/supervisor --backend claude --permission-mode <mode> --title "Supervisor (<name>)" --json
happier session set-permission-mode <id from the JSON above> <mode>
python3 -I ~/.claude/skills/supervisor/scripts/fleet.py send <id> <<'MSG'
Start up as the supervisor.
MSG
```

### 7. Verify

- `python3 -I ~/.claude/skills/supervisor/scripts/fleet.py snapshot` lists the session with the id from step 6, under this machine, flagged `SUPERVISOR`. Any session in a `~/supervisor` directory gets that flag, so match the id.
- Within a minute or two, `happier session history <id> --limit 5 --format compact` shows the supervisor read its skill and armed its watcher.

Then tell the user: open the Happier app, find "Supervisor (<name>)", and talk to it, starting with "what's running?". Describe a task to it the way they would describe it to a colleague. `/catchup` in that session summarises what changed since they last wrote there.

## Optional: more machines, and the hypervisor

- **Another machine:** repeat steps 1 to 7 there with its own profile name. To share state across machines, the user keeps `~/.claude` in a PRIVATE git repo with an auto-sync that commits and pushes it. That sync is not included; the README's Multi-machine section covers it. Add `config/gitignore.snippet` to that repo's `.gitignore`.
- **The hypervisor:** use it only with two or more supervisors, and on one machine only. Create `~/hypervisor/CLAUDE.md` as `~/.claude/skills/hypervisor/SKILL.md` describes, then start a Happier session there the same way as step 6, in `~/hypervisor` and titled "Hypervisor". Never run it in `~/supervisor`.

## Undo

Stop or archive the supervisor session in Happier, and delete `~/.claude/state/supervisor/` (the hooks go quiet without it) and `~/supervisor/`. To restore anything the installer replaced, copy it back from `~/.claude/backups/agent-supervisor-<timestamp>/`. To remove the skills and hooks, delete the paths the first install printed as `add` (the three skill directories, the three `hooks/*.sh` files from this repo, and their `hooks/tests/` files). Then remove the four hook entries it wired from `~/.claude/settings.json`.
