#!/bin/sh
# guard-watch-untracked.sh: wired into PreToolUse (Bash).
#
# BLOCKS starting the supervisor watcher (`fleet.py watch`) detached from the
# harness: with `&`, `nohup`, `disown`, `setsid`, `screen` or `tmux`.
#
# Why: the watcher's EXIT is what wakes the supervisor, and only a job started
# with the Bash tool's run_in_background is tracked, so only its exit re-invokes
# the session. A detached watcher wakes nobody, yet still holds watch.lock, so
# every correct re-arm then answers "already-armed" and the supervisor believes
# it is covered while it is blind. One supervisor did this three times on
# 2026-10-18, each caught only by checking ps (skill-gap report, 2026-10-19).
#
# Why a hook and not a check inside fleet.py: the script cannot tell a tracked
# job from an `&` child. The Bash tool's shell persists between calls, so an `&`
# child keeps the same live parent, and foreground calls write to a task output
# file just as background jobs do (both measured 2026-10-19).
#
# `fleet.py compact-self` is the one supervisor command that MUST run detached
# (/compact kills tracked jobs); it is not matched here. The parked doorbell
# (`watch --doorbell`) is detached too, but fleet.py starts it from inside a
# tracked watcher, never through this tool, and it differs from the case above
# on both counts: it wakes the supervisor (a Happier message) and every tracked
# arm retires it. Started by hand it is still blocked here.
#
# POSIX sh and standard text tools, plus jq or python3 to read the hook payload.

set +e

INPUT=$(cat)

if command -v jq >/dev/null 2>&1; then
    CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
elif command -v python3 >/dev/null 2>&1; then
    CMD=$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("tool_input",{}).get("command","") or "")
except Exception: pass' 2>/dev/null)
else
    exit 0
fi

# The watcher itself is matched in the RAW text, since `tmux new -d '<watch>'` quotes it, but
# only as the subcommand word right after fleet.py: a substring match also hit the word
# "watcher" in the documented compact-self --then text (2026-10-19).
printf '%s\n' "$CMD" | grep -Eq "fleet\.py[[:space:]]+watch([[:space:];&|)'\"]|\$)" || exit 0

# The detaching words are matched OUTSIDE quotes, so prose about the watcher (a commit
# message saying "never run fleet.py watch with & or nohup") passes.
# Heredoc bodies are data (a `fleet.py send <<'MSG'` message, a commit message file), so
# they are dropped. Then lines are joined: sed works per line, so a multi-line quoted commit
# message would otherwise keep its prose. Both measured 2026-10-19, when this hook blocked
# its own commit twice.
STRIPPED=$(printf '%s\n' "$CMD" | awk '
    skip != "" { t = $0; sub(/^[ \t]+/, "", t); if (t == skip) skip = ""; next }
    match($0, /<<-?[ \t]*["\047]?[A-Za-z_][A-Za-z0-9_]*["\047]?/) {
        d = substr($0, RSTART, RLENGTH); gsub(/^<<-?[ \t]*|["\047]/, "", d); skip = d
        print substr($0, 1, RSTART - 1); next
    }
    { print }' | tr '\n' ' ' | sed -e "s/'[^']*'/Q/g" -e 's/"[^"]*"/Q/g')

# `&&` is sequencing, not detaching: remove it before looking for a lone `&`.
# Redirections like `2>&1` and `&>` contain `&` too: remove those as well.
NOAND=$(printf '%s\n' "$STRIPPED" | sed -e 's/&&/ /g' -e 's/[0-9]*>&[0-9-]*/ /g' -e 's/&>>*/ /g')
DETACH=""
case "$NOAND" in
    *'&'*) DETACH="&" ;;
esac
for w in nohup disown setsid screen tmux; do
    if printf '%s\n' "$STRIPPED" | grep -Eq "(^|[^A-Za-z0-9_-])$w([^A-Za-z0-9_-]|\$)"; then
        DETACH="$w"
    fi
done

if [ -n "$DETACH" ]; then
    cat >&2 <<MSG
BLOCKED: fleet.py watch started detached ($DETACH).

A detached watcher wakes no one, and it still holds watch.lock, so every later
re-arm reports "already-armed" while the supervisor is blind (one supervisor,
three times on 2026-10-18).

Arm it as a harness-tracked job instead: run
  python3 -I ~/.claude/skills/supervisor/scripts/fleet.py watch
with the Bash tool's run_in_background: true and timeout: 7200000, and no &.
If one is already running detached, find it (pgrep -af 'fleet.py watch') and
stop it first.
MSG
    exit 2
fi

exit 0
