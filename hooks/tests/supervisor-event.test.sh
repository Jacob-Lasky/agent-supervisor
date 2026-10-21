#!/bin/sh
# Regression matrix for hooks/supervisor-event.sh and hooks/supervisor-plan-guard.sh (the hooks
# as the harness runs them), then the Python-level suite for the supervisor skill's scripts
# (supervisor_test.py).
#   sh hooks/tests/supervisor-event.test.sh
# Tests the tree it sits in: REAL is two directories above this file (the export package, or
# ~/.claude once installed). Uses a throwaway HOME so the real supervisor state is never touched.
REAL=$(cd "$(dirname "$0")/../.." && pwd); T=$(mktemp -d); fail=0
mkdir -p "$T/.claude/hooks" "$T/supervisor"
cp "$REAL/hooks/supervisor-event.sh" "$T/.claude/hooks/"; ln -s "$REAL/skills" "$T/.claude/skills"
H="$T/.claude/hooks/supervisor-event.sh"; SD="$T/.claude/state/supervisor"; LOG="$SD/events.jsonl"
ok(){ if eval "$2"; then echo "PASS :: $1"; else echo "FAIL :: $1"; fail=1; fi; }
fire(){ printf '%s' "$1" | HOME="$T" HAPPIER_SESSION_ID="$2" sh "$H"; echo $?; }
lines(){ [ -f "$LOG" ] && grep -c . "$LOG" || echo 0; }

ok "no state dir: exit 0" '[ "$(fire "{\"hook_event_name\":\"Stop\",\"session_id\":\"a\",\"cwd\":\"/tmp\"}" "")" = 0 ]'
ok "no state dir: nothing written" '[ ! -e "$T/.claude/state" ]'
mkdir -p "$SD"
fire '{"hook_event_name":"Stop","session_id":"s1","cwd":"/tmp","last_assistant_message":"done"}' h1 >/dev/null
ok "installed: Stop recorded with happier id" '[ "$(lines)" = 1 ] && grep -q "\"happier\":\"h1\"" "$LOG"'
fire "{\"hook_event_name\":\"Stop\",\"session_id\":\"sup\",\"cwd\":\"$T/supervisor\"}" h2 >/dev/null
fire "{\"hook_event_name\":\"Stop\",\"session_id\":\"sup\",\"cwd\":\"$T/supervisor/sub\"}" h2 >/dev/null
ok "supervisor dir excluded" '[ "$(lines)" = 1 ]'
fire "{\"hook_event_name\":\"Stop\",\"session_id\":\"x\",\"cwd\":\"$T/supervisor-old\"}" h3 >/dev/null
ok "sibling dir with same prefix still recorded" '[ "$(lines)" = 2 ]'
echo hsup > "$SD/self-session"
fire '{"hook_event_name":"Notification","notification_type":"permission_prompt","session_id":"sup","cwd":"/elsewhere"}' hsup >/dev/null
ok "supervisor excluded by its Happier id outside its dir" '[ "$(lines)" = 2 ]'
rm -f "$SD/self-session"
ok "malformed stdin: exit 0, nothing written" '[ "$(fire "not json" "")" = 0 ] && [ "$(lines)" = 2 ]'
BIG=$(python3 -c 'print("é"*20000 + " STATUS: done")')
fire "{\"hook_event_name\":\"Stop\",\"session_id\":\"s3\",\"cwd\":\"/tmp\",\"last_assistant_message\":\"$BIG\"}" "" >/dev/null
ok "huge message: one valid line within PIPE_BUF" '[ "$(lines)" = 3 ] && [ "$(tail -n1 "$LOG" | wc -c)" -le 4096 ] && tail -n1 "$LOG" | python3 -c "import json,sys;json.load(sys.stdin)"'
ok "huge message: the tail (STATUS) survives the clip" 'tail -n1 "$LOG" | grep -q "STATUS: done"'
printf '{"event":"Stop","happ' >> "$LOG"   # a fragment left by an interrupted writer
fire '{"hook_event_name":"Stop","session_id":"s4","cwd":"/tmp"}' h4 >/dev/null
ok "fragment costs only itself, not the next event" '[ "$(grep "\"happier\":\"h4\"" "$LOG" | python3 -c "import json,sys;print(json.load(sys.stdin)[\"session\"])")" = s4 ]'
G="$REAL/hooks/supervisor-plan-guard.sh"
guard(){ printf '%s' "$1" | HOME="$T" HAPPIER_SESSION_ID="$2" sh "$G"; echo "rc=$?"; }
ok "plan-guard: ordinary prompt is silent, exit 0" '[ "$(guard "{\"permission_mode\":\"auto\",\"cwd\":\"$T/supervisor\"}" h1)" = "rc=0" ]'
ok "plan-guard: no Happier id is silent" '[ "$(guard "{\"permission_mode\":\"plan\",\"cwd\":\"$T/supervisor\"}" "")" = "rc=0" ]'
ok "plan-guard: malformed stdin mentioning plan is silent" '[ "$(guard "permission_mode plan not json" h1)" = "rc=0" ]'
mkdir -p "$T/bin"; cat > "$T/bin/happier" <<'FAKE'
#!/bin/sh
echo "$*" >> "$HOME/happier.log"
case "$2" in
  actions) echo '{"ok":true,"data":{"result":{"items":[{"raw":{"meta":{"sentFrom":"android","permissionMode":"safe-yolo"}}}]}}}' ;;
  *) echo '{"ok":true,"data":{}}' ;;
esac
FAKE
chmod +x "$T/bin/happier"
OUT=$(printf '%s' "{\"permission_mode\":\"plan\",\"cwd\":\"$T/supervisor\"}" | HOME="$T" HAPPIER_SESSION_ID=hsup PATH="$T/bin:$PATH" sh "$G")
ok "plan-guard: resumed supervisor in plan gets its mode restored" 'grep -q "set-permission-mode hsup safe-yolo" "$T/happier.log"'
ok "plan-guard: and is told why, as UserPromptSubmit context" 'printf "%s" "$OUT" | python3 -c "import json,sys;d=json.load(sys.stdin)[\"hookSpecificOutput\"];assert d[\"hookEventName\"]==\"UserPromptSubmit\" and \"plan-mode guard\" in d[\"additionalContext\"]"'
rm -rf "$SD"
ok "plan-guard: no supervisor installed is silent" '[ "$(guard "{\"permission_mode\":\"plan\",\"cwd\":\"$T/supervisor\"}" h1)" = "rc=0" ]'
rm -rf "$T"

echo "-- supervisor_test.py"
OUT=$(python3 -I -W error::ResourceWarning "$REAL/hooks/tests/supervisor_test.py" 2>&1); rc=$?
printf '%s\n' "$OUT" | grep -E '\.\.\. (ok|FAIL|ERROR)$|^Ran |^OK|^FAILED'
[ "$rc" = 0 ] || { printf '%s\n' "$OUT" | grep -A15 -E '^(FAIL|ERROR):'; fail=1; }
exit $fail
