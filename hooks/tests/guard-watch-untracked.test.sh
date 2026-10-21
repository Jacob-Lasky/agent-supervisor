#!/bin/sh
# Regression matrix for hooks/guard-watch-untracked.sh. Run: sh hooks/tests/guard-watch-untracked.test.sh
# Tests the hook two directories above this file (the export package, or ~/.claude once installed).
# Kept in a FILE because the live guard inspects command text and would block
# an inline test that spells out the commands it guards against.
H="$(cd "$(dirname "$0")/.." && pwd)/guard-watch-untracked.sh"; W="python3 -I ~/.claude/skills/supervisor/scripts/fleet.py watch"; fail=0
t(){ printf '{"tool_input":{"command":%s}}' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" | sh "$H" >/dev/null 2>&1; r=$?
     if [ "$r" = "$2" ]; then echo "PASS rc=$r :: $1"; else echo "FAIL rc=$r want=$2 :: $1"; fail=1; fi; }
# Must block (2): the 2026-10-18 pattern and its siblings.
t "$W &" 2
t "$W > /tmp/w.log 2>&1 &" 2
t "nohup $W > /dev/null 2>&1 &" 2
t "nohup $W" 2
t "$W & disown" 2
t "setsid -f $W" 2
t "screen -dmS w $W" 2
t "tmux new -d '$W'" 2
t "cd ~ && $W &" 2
t "$W --max-wait 600 &" 2
t "setsid -f $W --doorbell" 2   # the doorbell is fleet.py's to start, never by hand
# Must allow (0): the tracked form, sequencing, redirections, and everything else.
t "$W" 0
t "$W --max-wait 6600" 0
t "$W 2>&1" 0
t "$W &> /tmp/w.log" 0
t "cd ~ && $W" 0
t "setsid -f python3 -I ~/.claude/skills/supervisor/scripts/fleet.py compact-self --then 'x'" 0
t "python3 -I fleet.py snapshot &" 0
# The exact compact-self line SKILL.md prescribes: "watcher" in --then is not the subcommand.
t "setsid -f python3 -I ~/.claude/skills/supervisor/scripts/fleet.py compact-self \\
    --focus \"Keep: open decisions and what the user said about them; anything promised to the user but not yet in the ledger.\" \\
    --then \"Compaction finished. Run <startup> from the supervisor skill, including re-arming the watcher.\" \\
    >/dev/null 2>&1" 0
t "python3 fleet.py watchdog-report &" 0
t "git commit -m \"never run fleet.py watch with & or nohup\"" 0
t "pgrep -af 'fleet.py watch'" 0
t "git commit -qm \"supervisor: block a detached fleet.py watch
(& nohup disown setsid screen tmux)\" && git log -1" 0
t "python3 -I fleet.py send abc <<'MSG'
never start fleet.py watch with nohup or &
MSG" 0
t "cat > /tmp/m <<EOF
fleet.py watch, tmux, setsid
EOF
git commit -F /tmp/m" 0
t "cat <<'EOF' > /tmp/x
hello
EOF
$W &" 2
t "sleep 5 &" 0
exit $fail
