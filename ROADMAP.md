# Roadmap: build a per-machine agent supervisor from scratch

This guide teaches how to rebuild the system in this package for your own setup, in whatever language or session relay you use. The shipped files are one working implementation; this is the reasoning that shapes them.

## What it is, and the one core idea

A long-lived "supervisor" coding-agent session is the only session the user talks to on a machine. It delegates every piece of work to short-lived worker sessions, watches them, answers what it may, verifies what they claim, and escalates only what needs the human.

**The core idea: the supervisor's conversation is disposable; its state lives on disk and in the live session inventory.** Every rule follows from that. A restart, a compaction or a crash must be a non-event, which means nothing important may live only in the model's context. Second idea, equally load-bearing: **waiting must cost zero tokens.** A supervisor that polls with model turns burns its whole budget rereading a large context to learn that nothing changed.

## Prerequisites to have first

- **A session relay with a CLI** that can list sessions (with host, path, title, active state), create a session in a directory with a permission mode, send a message to a session, read its history, archive and unarchive it, and set its title and mode. This package uses Happier. Whatever you use, measure each behaviour before relying on it (see Gotchas: several are surprising).
- **A local session listing with live state** (busy, idle, waiting). Claude Code's `claude agents --json` gives it.
- **Agent hooks**: an event on turn end (with the final message), on notifications (permission prompts), on each user prompt, and before each tool call (able to block). Claude Code's `Stop`, `Notification`, `UserPromptSubmit` and `PreToolUse`.
- **Background jobs whose exit re-invokes the agent.** In Claude Code, a Bash call with `run_in_background: true`; its exit is a new turn. This is the wake mechanism.
- Optional: a private git repo syncing the agent config directory across machines, for multi-machine visibility.

## File layout

| File | Role |
| --- | --- |
| `skills/supervisor/SKILL.md` | The operating rules: startup, watch loop, delegation, authority, verification, reaching the user, close-out, setup, anti-patterns. |
| `skills/supervisor/profiles/<machine>.md` | Per-machine facts: host match, worker permission mode, what work belongs here, routing to other machines, file search, local traps. Procedure and machine data stay separate. |
| `skills/supervisor/scripts/ledger.py` | Append-only work log, folded into items; renders a local view and a whitelisted published summary. |
| `skills/supervisor/scripts/fleet.py` | Inventory, the watcher, safe send, archive/park/reopen/orphan, catch-up, self-compaction, the hook entry points. |
| `hooks/*.sh` | Thin, always-exit-0 shell shims that call `fleet.py record` / `plan-guard`, plus a guard that blocks detaching the watcher. |
| `hooks/tests/` | Regression suites that run against a throwaway HOME with the relay and agent CLIs stubbed. |
| `skills/hypervisor/SKILL.md` | A separate session that audits the supervisors and owns their rules, scripts and tests. |
| `skills/catchup/SKILL.md` | "What changed since I last typed here", grouped by what the user must do. |

## Core methodology

1. **Ledger as event log.** Append JSON lines (`add`, `set`, `decide`, `resolve`) under a file lock; derive state by folding. Never edit in place. Items carry title, objective, pass/fail criteria, owners (session ids), state, evidence list, next step, notes, decisions, parked sessions. States: `queued, working, needs-input, ready-for-review, done, failed, stopped`. Refuse to close an item with an open decision. Allocate ids under the lock (concurrent adds raced to the same id).
2. **Published summary by whitelist.** Render a second view with only id, state, title, next, latest evidence and open decision question/recommendation; cap each field; redact credential shapes; HTML-escape (a `<!--` in a field hid the rest of the page). Write atomically, and skip the write when only the timestamp would change (each write is a sync commit). One file per machine, so peers never conflict.
3. **Event capture by hook.** On `Stop` and `Notification`, append one compact line (session id, relay id from the environment, cwd, type, the final message clipped to keep head AND tail, since workers put STATUS and evidence at the end). One `write()` under 4096 bytes with a leading newline, so concurrent writers never interleave and a torn fragment costs only itself. Exclude the supervisor's own directory and its own relay id, or it wakes itself.
4. **The watcher.** A single process under an exclusive lock tails the event log and, every 60 s, polls both session listings. It exits (printing one JSON line) only when something needs the supervisor:
   - an owned worker's turn ended (hook, or a busy-to-idle transition the poll saw, confirmed against history after a grace period);
   - any local permission prompt;
   - an owned worker gone; a peer supervisor gone;
   - stuck clocks: busy too long, idle after a turn with no `STATUS:` line, idle too long after an interim `STATUS: working` turn, a prompt pending too long, the config sync not pushing. Each fires once per condition.
   Everything else is folded into the next wake as context. Identify a turn by the head and tail of its final message, so a hook and a poll reporting the same turn wake once.
5. **Liveness and parking.** The harness kills background jobs at a hard limit, so the watcher exits on its own just before it (`rearm`: re-arm silently, no text). If nothing owned is busy or waiting, it instead hands its lock to a detached "doorbell" copy with no time limit, which on its first real wake saves what it saw and sends the supervisor one fixed message. The next tracked arm delivers the saved wake and retires the doorbell. A quiet day then costs one turn.
6. **Delegation discipline.** Record the item before spawning. Brief = objective, directory, criteria verbatim, boundaries, the bar for asking, and the required final line `STATUS: done|blocked|question` (or `STATUS: working (waiting on X)` for interim turns). Name the existing user-facing surface to extend. One writer per repo area. The supervisor may make one bounded read to route a question; anything more is a worker's job.
7. **Authority.** Answer content, never permission. Questions reach the user only as one of four kinds (outbound, security, taste, action) with a non-empty account of what was tried; enforce it in `decide`. Treat worker text as data: nothing in a wake is an approval. Send messages as an argv element from stdin, never through a shell string.
8. **Verification.** A "done" is a claim; check each criterion against evidence you can see (CI, test output, a live endpoint) and record it before telling the user. A reviewer with fresh context passes a PR before it is called ready.
9. **Close-out.** Archive a worker the same turn its item is done and evidenced, or stopped with a reason written with the stop. Park an open item's idle worker when the item waits on something external; reopen it for the follow-up so it keeps its context. Never archive a supervisor, the hypervisor, a worker that still owns another open item, or anything not positively known to be between turns.
10. **Self-compaction.** Report the supervisor's own context size on every wake and flag it past a threshold. Compact from a detached helper that waits until the session is idle twice in a row, sends `/compact`, waits for it to finish, then sends a "run startup and re-arm" message.

## The main flow (pseudocode)

```
startup():
    show ledger; snapshot sessions; reconcile owned-but-gone and idle-without-report
    arm watcher (tracked background job, max timeout)
    restate open decisions once

on wake(w):
    if w.kind == "rearm":  re-arm; end turn with no text
    if w.kind == "parked": end turn with no tool call and no text
    if w.kind == "already-armed": end turn (another tracked watcher holds the lock)
    for each event/change in w:
        classify worker text: done -> verify -> evidence -> report + archive
                              question -> answer from code/docs, or challenge, or decide(kind, tried)
                              working -> leave it;  stuck -> steer once, then escalate
    update ledger
    if w.compact: last command of the turn = detached compact-self
    else: re-arm

watch():
    take lock (or retire a doorbell and take it, or print already-armed)
    loop:
        read new events; owned Stop / any permission prompt -> wake
        every backstop seconds (never within a poll budget of the deadline):
            list sessions; untrusted listing = failed fetch for that source
            diff against last fleet: gone only after 2 successful listings miss it;
                a listing that drops every known session is doubted once
            update stuck clocks for owned local sessions
        poll-seen finishes past their grace: compare history's latest turn to delivered turns
        stuck checks; sync check
        if wake: print JSON, then save offsets (print first: a kill repeats a wake, never loses one)
        elif deadline: park if quiet else print rearm
```

## Gotchas learned the hard way

- **A detached watcher is a blind supervisor.** It wakes no one but holds the lock, so every correct re-arm reports "already armed". The script cannot tell a tracked job from an `&` child; block the detaching forms with a pre-tool hook that matches outside quotes and heredocs (it blocked its own commit messages twice before that).
- **One bad listing reported every worker gone on two machines at once.** Absence is evidence only from a complete, well-formed listing that includes the caller's own session, confirmed by a second listing. Key a source's failure by a fixed label, not one derived from the binary's path (an absolute path silently broke the match).
- **A message sent to a busy session is injected into the running turn as text.** `/compact` sent mid-turn did not compact and derailed the task. Only send slash commands to an idle session.
- **`/compact` kills the session's tracked background jobs**, including the watcher; the post-compaction turn must re-arm it.
- **A session created from a plan-mode session starts in plan mode**, and a relay resume can restart a session in plan mode. Create without a prompt, set the mode, then send; add a prompt hook that tells the model the plan mode was not requested.
- **Changing a session's stored mode does not change its running process.** It applies at the next start.
- **The session listing records host at creation, forever.** A containerized machine's hostname changes on recreate; remember every hostname the machine has had.
- **Only a message the user typed counts as their answer.** Every script and peer message looks like a user message; use the relay's sender tag, with an allow-list of app values, both for the "already answered" guard and for the catch-up cut-off. Session history pages cap out (250 rows was 40 minutes of a busy supervisor); page the transcript API instead.
- **Workers relay questions they could answer.** Six in one day, four answerable by a probe. Make the question kinds and the "what was tried" field mandatory in the tool, not just the prose.
- **A supervisor that "just reads" ends up implementing.** Four ssh probes, two hung, in six minutes. Cap it at one bounded read and hand off.
- **Context size is the price of every wake.** A "compact readily" rule alone left a supervisor at 400k to 540k tokens per call for days; a flag on the wake made it a mechanism.
- **Auto-mode classifiers judge command text.** A ledger write that described a destructive step in its `--next` was denied; keep destructive specifics in the worker's brief.
- **Concurrent heavy builds from several workers can take a machine down.** A written limit did not hold; a slot-lock wrapper plus a blocking hook did.
- **Stopped items without a reason kept their workers open forever.** Require the reason in the same write as the state change.

## Build order

1. `ledger.py` with `add/set/show` and the fold. Test concurrency of `add`.
2. `fleet.py snapshot`: merge the relay listing with the local agent listing.
3. The `record` hook and a watcher that wakes on owned `Stop` and permission prompts only. Arm it as a tracked background job and confirm the exit re-invokes the session.
4. The supervisor SKILL.md: startup, briefs with `STATUS:` lines, authority, verification. Run one machine for a few days.
5. Backstop polling with the trust rules (complete listing, two-miss confirmation, drop doubt) and the finish-without-hook path.
6. Stuck clocks; `decide` with kinds and `--tried`; `send --guard`.
7. `archive`, `park`, `reopen`, `orphan` with their refusal rules.
8. Liveness exit, then the parked doorbell.
9. Self-compaction and the context flag; the plan-mode guard hook; the detach guard hook.
10. Published summary and a second machine; then the hypervisor and catch-up.

Every step lands with a regression test run against a throwaway HOME, shown failing first.
