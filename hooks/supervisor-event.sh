#!/bin/sh
# supervisor-event.sh: wired into Notification and Stop. Appends one compact line per
# event to this machine's supervisor event log, which skills/supervisor/scripts/fleet.py
# watch tails to wake the standing supervisor session at zero token cost.
# Silent no-op unless a supervisor is installed here (ledger.py init creates the state dir).
# MUST exit 0 and stay fast: it runs in every Claude session's hook chain on this machine.
# State lives under ~/.claude/state, NOT $XDG_STATE_HOME: Happier points XDG_STATE_HOME at a
# per-session isolation dir, so each session would log somewhere the watcher never reads.
[ -d "${HOME}/.claude/state/supervisor" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 -I "${HOME}/.claude/skills/supervisor/scripts/fleet.py" record 2>/dev/null
exit 0
