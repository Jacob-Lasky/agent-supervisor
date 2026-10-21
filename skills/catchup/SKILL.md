---
name: catchup
description: "Catch the user up in a supervisor or hypervisor session: a short summary of what they need to know since they last typed to that session, covering decisions waiting on them, PRs ready for review, work that finished or failed, and work that started. Fire on /catchup, or when the user, talking to a supervisor or the hypervisor, asks what they missed, what changed, where things stand since they last checked, or to bring them up to speed."
allowed-tools: Bash, Read
---

## Requirements

- **The `supervisor` skill from the same package**, installed at `~/.claude/skills/supervisor/` (this skill runs its `fleet.py catchup` and reads its ledger), with every requirement it lists. It is only useful inside a supervisor or hypervisor session that runs under Happier.
- **Optional**: the `hypervisor` skill, for a whole-fleet catch-up, and a git-synced `~/.claude` for other machines' sections.

## Install

Copy this directory to `~/.claude/skills/catchup/`. Skills load in the NEXT Claude Code session; confirm with `/help`.

<objective>
The user lets their supervisors run unattended, then comes back. Give them, in under a minute of reading, what changed since their last message to this session and what is waiting on them. Lead with anything that needs them. Everything they can safely ignore gets one line at most, or nothing. A supervisor (`~/supervisor`, the `supervisor` skill) covers its own machine. The hypervisor (`~/hypervisor`, the `hypervisor` skill) covers every machine: its catch-up is the one to use when the user wants the whole fleet at once. In any other session, say that and stop.
</objective>

<execution>
1. **Get the changes**: `python3 -I ~/.claude/skills/supervisor/scripts/fleet.py catchup`. It finds the user's last message from Happier's sender tag. Messages from peers, the hypervisor and scripts are tagged `cli` and do not count, and the message asking for this catch-up is skipped. It then prints that message and the ledger changes since, grouped as NEEDS YOU / READY FOR REVIEW / FINISHED / STARTED / PROGRESSED / UNCHANGED.
   - In the hypervisor, add `--fleet`. This machine still comes from its full ledger. Every other machine comes from its published summary (`supervisors/<profile>.md`), diffed through git between the cut-off and now, so it shows only the latest evidence and next step per item. The output ends with the supervisor-system commits since the cut-off. Write one section per supervisor, each in the order of step 4, and reduce a supervisor with nothing to report to a one-line "nothing new" (2026-10-19).
   - If the user named a window ("since this morning", "since yesterday 5pm"), pass `--since <ISO time>` instead.
   - Exit 1 prints why: no Happier session, an unreadable transcript, or no earlier message from the user. Rerun with `--since`, using their window if they gave one and otherwise the last 12 hours, and say which window you used.
2. **Add what the ledger cannot know**: `fleet.py snapshot`. An owned worker flagged `PENDING`, or one waiting on a permission prompt, is blocked on the user right now. List it under needs-you with the exact action it wants. The hypervisor sees peers' sessions only as `remote`. Their blocked state is in their published `next` and decisions, or ask the peer.
   An item whose `next` is an action of the user's ("User to send...") is listed under NEEDS YOU even when its state is `working`. In a supervisor, set that item to `needs-input` so the ledger says so too. The hypervisor tells the owning supervisor instead.
3. **Read before you summarize**, but only for items in NEEDS YOU or READY FOR REVIEW whose evidence line is unclear. Use `ledger.py show` or the PR itself. Do not re-verify FINISHED items: the supervisor recorded their evidence when it closed them (its `<verifying>` rules).
4. **Write the summary** in this order, leaving out any empty section:
   - **Needs you**: one line per decision or blocker, with the question, the recommendation and any URL. One question at a time; if the user must pick among several options, give a short numbered list with your recommendation first.
   - **Ready for review**: the PR URL and one line on what it does.
   - **Finished**: the outcome in one line each. A stopped or failed item gets its reason.
   - **Started / moving**: one line in total unless something changed direction. Unchanged items are a count, or nothing.

   Use plain English and titles, not session ids or bare W-numbers. Say nothing about watchers, ledgers or wakes.
</execution>

<anti_patterns>
- **Counting any user message as the user's.** Peers, the hypervisor and compact-self all write into the supervisor as user messages. Only `fleet.py catchup`'s `sentFrom` test separates them. Picking the cut-off by eye from history starts the window at the last peer message and hides hours of changes.
- **Using `happier session history` for the cut-off.** It stops at 250 rows, about 40 minutes of a busy supervisor (2026-10-19), so the cut-off silently lands on whatever is oldest in the window.
- **Replaying the timeline.** The user wants the state and what needs them, not a chronological log of wakes.
</anti_patterns>
