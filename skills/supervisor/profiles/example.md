# Profile: example (TEMPLATE: copy to profiles/<name>.md for each machine and fill it in)

<!-- A profile is any file profiles/<name>.md; <name> (lowercase letters, digits, - and _) is what
`ledger.py init --profile <name>` accepts and what names this machine's published summary,
supervisors/<name>.md. Keep one file per machine, e.g. laptop.md and devbox.md. Every
<PLACEHOLDER> below is yours to replace; delete any optional section you do not need. -->

- **Host match**: Happier `host` is `<HOSTNAME>` (what `hostname` prints on this machine). If this machine's agents run inside a container whose hostname is its container id, the id changes on every recreate: describe the match as "host is none of the other profiles' hosts", never a literal id. `fleet.py` remembers every hostname this machine has had in `~/.claude/state/supervisor/host-aliases`.
- **Home / launcher**: `<HOME>/supervisor` (created by `ledger.py init`). Daemon-spawned sessions may default to a different directory, so always pass `--path` to `happier session create`. If non-interactive shells on this machine do not have `happier`, `claude` or `codex` on PATH, write the export line here, e.g. `export PATH="$HOME/<TOOL_BIN_DIR>:$PATH"`.
- **Worker permission mode**: `<MODE>` for Claude workers. Pick deliberately:
  - `yolo` (Happier maps it to Claude `bypassPermissions`) is the low-friction choice, and it is only safe when your PreToolUse hooks are the boundary for what cannot be undone: messages that reach another person, pushes to shared repos, destructive commands. Measured 2026-10-20: a PreToolUse hook returning `ask` still blocked a Bash call under `bypassPermissions`. In `yolo` the supervisor still cannot approve anything, but a classifier no longer stops workers on actions the supervisor is not allowed to approve.
  - `auto` (Happier echoes it as `safe-yolo`; for Claude it is the classifier-gated auto mode) is the choice when you have no such hooks.
  - **Codex workers stay `auto`** (Codex's `workspace-write` sandbox) whatever you pick: Codex runs no Claude hooks, so in `yolo` nothing would stop one messaging a person or pushing.
  - If this is someone's live interactive machine, say here which kinds of brief still need the user's yes first whatever the mode (system packages, display, audio, boot config).
- **Owns**: `<WHAT WORK BELONGS ON THIS MACHINE>`: which repos, services, credentials and kinds of task. Example: "the `<REPO_A>` and `<REPO_B>` checkouts under `~/src`, the local docker daemon, cloud CLI work against `<ACCOUNT>`."
- **Routes to `<OTHER_PROFILE>`**: `<WHAT GOES TO THAT MACHINE'S SUPERVISOR>`. Include anything that turns out to be that machine's while diagnosing from here: once evidence points there, the other supervisor gets it with what is known so far, and this one never ssh-probes into it. Repeat this line for each other machine; delete it if you run a single supervisor.
- **Search**: how to find a file by name on this machine. `locate` (plocate) is the default; say where its database is, how often it is rebuilt, and which paths it does not index (`/tmp`, container filesystems, `.git`, a btrfs `/home` that plocate skips as a bind mount unless `PRUNE_BIND_MOUNTS="no"`). If `locate` is not installed, say what to use instead, and say which trees must never be walked with `find` or `grep -r` (a slow network or FUSE mount can jam the machine).
- **Traps**: what bit a supervisor or worker on this machine. Common ones:
  - Daemon-spawned sessions may lack PATH entries, `DISPLAY` / `WAYLAND_DISPLAY`, or credentials an interactive shell has. To start a GUI app from a session on a systemd desktop, go through the user manager: `systemd-run --user --quiet --collect --unit=app-<name>-$(date +%s) <cmd>`.
  - A machine that sleeps shows its supervisor missing from `fleet.py snapshot`: suspended, not broken.
  - Cap memory on container test runs (`--memory=4g --memory-swap=4g`) if the machine also hosts live services.

## Optional: a heavy-build gate

Use this section only if several workers on this machine compile or build images at once. On 2026-10-19, concurrent compiler builds from several workers stalled one machine twice: the compiler was OOM-killed, the VPN client logged out, and every Happier session lost its relay for over ten minutes. A written "at most two builds" rule did not hold, so the fix had to be a mechanism:

- **One wrapper in front of every heavy command** (compiles, test builds, image builds): a small script that takes one of N slot locks (`flock` on `/tmp/<name>-slot-<n>.lock`) for the command's lifetime, waits until available memory and load are under a threshold before starting, runs the command attached (never detached), and passes its exit code through. An image build takes every slot.
- **A PreToolUse hook that refuses** a heavy command that is not prefixed by the wrapper, so Claude workers cannot skip it. Codex workers run no Claude hooks, so their briefs must say it in words.
- **Brief rule**: a worker that cleans up `/tmp` must not delete the slot lock files, or the next build takes a fresh, unheld lock.

Record here: the wrapper's name, its slot count, its thresholds, and how to see who holds the slots.
