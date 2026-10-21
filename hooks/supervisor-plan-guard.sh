#!/bin/sh
# supervisor-plan-guard.sh: wired into UserPromptSubmit. When a supervisor or a worker it owns
# receives a message while Claude is in plan mode that the message did not ask for (a Happier
# resume does this, measured 2026-10-19), restore the session's Happier mode and tell the model why.
# Silent no-op unless a supervisor is installed here and this is a Happier session.
# MUST exit 0 and stay fast: it runs on every prompt in every Claude session on this machine.
[ -d "${HOME}/.claude/state/supervisor" ] || exit 0
[ -n "${HAPPIER_SESSION_ID}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
payload=$(cat)
# Cheap pre-filter so ordinary prompts never start Python. Loose on purpose: a false match
# only costs one Python start, and fleet.py re-checks the parsed field.
case "$payload" in *permission_mode*plan*) ;; *) exit 0 ;; esac
printf '%s' "$payload" | python3 -I "${HOME}/.claude/skills/supervisor/scripts/fleet.py" plan-guard 2>/dev/null
exit 0
