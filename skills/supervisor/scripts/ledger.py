#!/usr/bin/env python3
"""Supervisor work ledger: an append-only event log plus derived views.

The log (ledger.jsonl in the machine's supervisor state dir) is the record;
ledger.md is regenerated from it and never edited by hand. A fresh supervisor
rebuilds its picture of the work from this log plus live inventory, so nothing
important may live only in the supervisor's conversation.

  ledger.py init --profile NAME      NAME is a profile with a file profiles/NAME.md
  ledger.py add --title T --objective O [--criteria C ...] [--owner SESSION] [--repo PATH]
  ledger.py set ID [--state S] [--owner SESSION] [--evidence E] [--next N] [--note TEXT]
  ledger.py decide ID --kind K --tried T --question Q --recommendation R
  ledger.py resolve ID --answer A
  ledger.py show [--all]            open items and open decisions (default)
  ledger.py owned                   session ids of in-flight items, one per line
  ledger.py render                  rewrite ledger.md and the published summary from the log

Every rewrite of ledger.md also writes ~/.claude/supervisors/<profile>.md, a whitelisted summary
(ID, state, title, next, latest evidence, open decisions; each field truncated). If ~/.claude is
synced through a PRIVATE git repo, that sync carries it, so every machine's work list can be read
in one place. One file per machine, so two machines never write the same file. ledger.jsonl and
events.jsonl stay local.
"""
import argparse
import contextlib
import fcntl
import html
import json
import os
import re
import socket
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "state", "supervisor")
LOG = os.path.join(STATE_DIR, "ledger.jsonl")
VIEW = os.path.join(STATE_DIR, "ledger.md")
SUP_HOME = os.path.join(os.path.expanduser("~"), "supervisor")
PUBLISH_DIR = os.path.join(os.path.expanduser("~"), ".claude", "supervisors")  # tracked if ~/.claude is a synced repo
# A profile is any profiles/<name>.md shipped beside scripts/. The name becomes a file name
# (supervisors/<name>.md), so it is also held to a plain slug.
PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TEMPLATE_PROFILE = "example"  # the shipped template: copy it to profiles/<name>.md, never init with it


def profile_names():
    """Profile names with a profiles/<name>.md, sorted; empty if the directory is missing."""
    try:
        files = os.listdir(PROFILE_DIR)
    except OSError:
        return []
    return sorted(f[:-3] for f in files if f.endswith(".md") and PROFILE_NAME.fullmatch(f[:-3]))

# The published summary may be pushed to a git host, so it carries ONLY these fields, each capped. DO NOT add
# notes, objective, criteria, owners or decision answers: notes hold worker output (fleet.py
# records archive results there) and the rest is not needed to see where work stands.
PUBLISH_LIMITS = {"id": 20, "state": 20, "title": 120, "next": 200, "evidence": 240, "question": 300, "recommendation": 300}
CLOSED_SHOWN = 20  # most recent closed items published; older ones stay in the local ledger
# Credential shapes scrubbed from published text: GitHub, Anthropic/OpenAI, Slack (bot/user/app),
# AWS (long-term and session), Bitwarden access tokens. A backstop only: an opaque key has no
# shape, so the control is the field whitelist plus <record>'s write-for-publication rule.
# The 16-char floor keeps short labels such as `sk-benchmark-v1` readable.
SECRET = re.compile(r"\b(?:(?:gh[pousr]_|github_pat_|sk-|xox[a-z]-|xapp-|0\.[0-9a-f]{8}-)[A-Za-z0-9_.:+/=\-]{16,}"
                    r"|(?:AKIA|ASIA)[A-Z0-9]{16}\b)")

# Industry-convergent state vocabulary (Claude agent view, Codex app, Devin, Jules, Copilot).
STATES = ("queued", "working", "needs-input", "ready-for-review", "done", "failed", "stopped")
TERMINAL = ("done", "failed", "stopped")

LAUNCHER = """# Standing supervisor ({profile})

You are the user's standing supervisor for this machine. At session start, and again after any
compaction or restart, read `~/.claude/skills/supervisor/SKILL.md` and follow it with
profile `{profile}`. The ledger and event log under `~/.claude/state/supervisor/` are the
record of the work; your conversation is not.
"""


@contextlib.contextmanager
def write_lock():
    """Serialize ledger writers (ledger.py and fleet.py archive/reopen): two concurrent adds
    would otherwise fold the same log and both take W<n>."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "ledger.lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def append(entry):
    os.makedirs(STATE_DIR, exist_ok=True)
    entry = {"ts": int(time.time()), **entry}
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def fold(until=None):
    """Replay the log into {id: item}. Unknown ops are ignored so the log can grow.
    until: replay only entries with ts <= until, i.e. the ledger as it stood then."""
    items = {}
    if not os.path.exists(LOG):
        return items
    with open(LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if until is not None and e.get("ts", 0) > until:
                continue  # not break: a clock step can put an older ts after a newer one
            op, iid = e.get("op"), e.get("id")
            if op == "add":
                items[iid] = {
                    "id": iid, "title": e["title"], "objective": e.get("objective", ""),
                    "criteria": e.get("criteria", []), "repo": e.get("repo"),
                    "state": "queued", "owners": [], "evidence": [], "next": "",
                    "notes": [], "decisions": [], "created": e["ts"], "updated": e["ts"],
                    "parked": [], "reason": "",
                }
                if e.get("owner"):
                    items[iid]["owners"].append(e["owner"])
                continue
            it = items.get(iid)
            if it is None:
                continue
            it["updated"] = e["ts"]
            if op == "set":
                if e.get("state"):
                    it["state"] = e["state"]
                    # Why an item was stopped or failed is the note written WITH that state
                    # change; a later script note (released, parked, reopened) is not a reason.
                    it["reason"] = (e.get("note") or "") if e["state"] in TERMINAL else ""
                # fleet.py park / reopen: an archived worker the item will come back to.
                if e.get("park") and e["park"] not in it["parked"]:
                    it["parked"].append(e["park"])
                if e.get("unpark") in it["parked"]:
                    it["parked"].remove(e["unpark"])
                if e.get("owner") and e["owner"] not in it["owners"]:
                    it["owners"].append(e["owner"])
                if e.get("drop_owner") in it["owners"]:
                    it["owners"].remove(e["drop_owner"])
                if e.get("evidence"):
                    it["evidence"].append(e["evidence"])
                if e.get("next") is not None:
                    it["next"] = e["next"]
                if e.get("note"):
                    it["notes"].append(e["note"])
            elif op == "decide":
                it["decisions"].append({"question": e["question"], "recommendation": e["recommendation"],
                                        "kind": e.get("kind"), "tried": e.get("tried"),
                                        "asked": e["ts"], "answer": None})
            elif op == "resolve":
                for d in it["decisions"]:
                    if d["answer"] is None:
                        d["answer"] = e["answer"]
                        break
    return items


# The only questions that may reach the user (2026-10-21, after a supervisor relayed six "needs
# the user" questions from one worker unchallenged, and the user asked why it could not find the
# answers itself: two were answerable from the deployment and chat history, one by a live test,
# one by a check run). Anything else goes back to the worker to probe. --tried is what was probed and why it did not settle the question, so
# the bar is checked when the decision is written, not remembered when it is relayed.
DECISION_KINDS = {
    "outbound": "a message, post or comment that reaches another person (the draft is in --tried)",
    "security": "a security, permission, credential or destructive/irreversible call",
    "taste": "taste, priority, budget or a commitment to a person that no probe can settle",
    "action": "something only the user can physically do: reboot their machine, log in, click in a UI",
}
TRIED_MIN = 20  # chars: "none" or "n/a" is not an account of what was tried


def owned_sessions(items):
    """Session ids (Happier or Claude) attached to in-flight items."""
    return {sid for it in items.values() if it["state"] not in TERMINAL for sid in it["owners"]}


def next_id(items):
    n = max((int(i[1:]) for i in items if i[1:].isdigit()), default=0)
    return f"W{n + 1}"


def open_decisions(it):
    return [d for d in it["decisions"] if d["answer"] is None]


def age(ts):
    s = int(time.time()) - ts
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"


def describe(it):
    lines = [f"{it['id']} [{it['state']}] {it['title']}  (updated {age(it['updated'])} ago)"]
    if it["owners"]:
        lines.append(f"    sessions: {', '.join(it['owners'])}")
    if it["parked"] and it["state"] not in TERMINAL:
        lines.append(f"    parked: {', '.join(it['parked'])} (fleet.py reopen {it['id']} <id> when the next step is met)")
    if next_step(it):
        lines.append(f"    next: {next_step(it)}")
    if it["evidence"]:
        lines.append(f"    evidence: {it['evidence'][-1]}")
    for d in open_decisions(it):
        kind = f" [{d['kind']}]" if d.get("kind") else ""
        lines.append(f"    OPEN DECISION{kind} ({age(d['asked'])}): {d['question']} | recommend: {d['recommendation']}")
        if d.get("tried"):
            lines.append(f"        tried: {d['tried']}")
    return "\n".join(lines)


def cell(text, limit=None):
    """One table-safe line: no pipes or newlines, optionally capped with an ellipsis."""
    text = " ".join(str(text or "").split()).replace("|", "/")
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def table(rows, cols):
    """Markdown table; cols is [(header, fn(item) -> str)]."""
    out = ["| " + " | ".join(h for h, _ in cols) + " |", "|" + " --- |" * len(cols)]
    out += ["| " + " | ".join(fn(it) for _, fn in cols) + " |" for it in rows]
    return out + [""]


def next_step(it):
    """An item's next step; a closed item has none, whatever it last recorded."""
    return "" if it["state"] in TERMINAL else it["next"]


def by_recency(items, keep):
    return sorted((it for it in items.values() if keep(it["state"])), key=lambda i: -i["updated"])


def render(items):
    out = ["# Supervisor ledger", "", f"_Generated {time.strftime('%Y-%m-%d %H:%M %Z')} from ledger.jsonl; do not edit._", ""]
    pending = [it for it in items.values() if open_decisions(it)]
    if pending:
        out += ["## Open decisions", ""]
        for it in pending:
            for d in open_decisions(it):
                out.append(f"- **{it['id']} {it['title']}**: {d['question']} Recommendation: {d['recommendation']} (asked {age(d['asked'])} ago)")
        out.append("")
    cols = [("ID", lambda i: i["id"]), ("State", lambda i: i["state"]), ("Title", lambda i: cell(i["title"])),
            ("Sessions", lambda i: cell(", ".join(i["owners"] + [f"parked {p}" for p in i["parked"]
                                                                     if i["state"] not in TERMINAL]))),
            ("Next", lambda i: cell(next_step(i))),
            ("Latest evidence", lambda i: cell((i["evidence"] or [""])[-1]))]
    for heading, keep in (("In flight", lambda s: s not in TERMINAL), ("Closed", lambda s: s in TERMINAL)):
        rows = by_recency(items, keep)
        if rows:
            out += [f"## {heading}", ""] + table(rows, cols)
    with open(VIEW, "w") as f:
        f.write("\n".join(out))
    try:
        publish(items)
    except (OSError, ValueError) as e:
        # The ledger write already happened and is the record; a failed copy must not abort
        # the caller (fleet.py archive is mid-loop under the lock). Say so loudly instead.
        print(f"ledger: could not publish the summary: {e}", file=sys.stderr)
    return VIEW


def profile():
    """This machine's profile; None before `init`. A profile file that exists but names no
    profiles/<name>.md raises ValueError, so render() reports it instead of silently going quiet."""
    try:
        with open(os.path.join(STATE_DIR, "profile"), encoding="utf-8") as f:
            p = f.read().strip()
    except FileNotFoundError:
        return None
    if p not in profile_names():
        raise ValueError(f"profile file holds {p[:40]!r}, which has no profiles/<name>.md "
                         f"(have: {', '.join(profile_names()) or 'none'}); re-run ledger.py init")
    return p


def pub(text, field):
    """A published cell: credential shapes redacted, capped, then HTML-escaped so a `<!--` or
    `<details>` in a field cannot hide or reshape the rest of the page on a git host's renderer."""
    return html.escape(cell(SECRET.sub("[redacted]", str(text or "")), PUBLISH_LIMITS.get(field)), quote=False)


def published_header(prof, ts):
    return [f"# Supervisor ({prof}) on {socket.gethostname()}",
            f"_Generated {ts} by ledger.py from this machine's local ledger.jsonl, which is the source of truth. "
            "Do not edit; it is rewritten on every change._", ""]


def publish_body(items):
    out = []
    pending = [(it, d) for it in by_recency(items, lambda s: True) for d in open_decisions(it)]
    if pending:
        out += ["## Open decisions", ""]
        out += [f"- **{pub(it['id'], 'id')} {pub(it['title'], 'title')}**: {pub(d['question'], 'question')} "
                f"Recommendation: {pub(d['recommendation'], 'recommendation')}" for it, d in pending]
        out.append("")
    cols = [("ID", lambda i: pub(i["id"], "id")), ("State", lambda i: pub(i["state"], "state")),
            ("Title", lambda i: pub(i["title"], "title")), ("Next", lambda i: pub(next_step(i), "next")),
            ("Latest evidence", lambda i: pub((i["evidence"] or [""])[-1], "evidence"))]
    live, closed = by_recency(items, lambda s: s not in TERMINAL), by_recency(items, lambda s: s in TERMINAL)
    out += ["## In flight", ""] + (table(live, cols) if live else ["Nothing in flight.", ""])
    if closed:
        out += ["## Recently closed", ""] + table(closed[:CLOSED_SHOWN], cols)
        if len(closed) > CLOSED_SHOWN:
            out += [f"{len(closed) - CLOSED_SHOWN} older closed item(s) are in the local ledger only.", ""]
    return "\n".join(out)


def publish(items):
    """Write ~/.claude/supervisors/<profile>.md; returns its path, or None before `init`.
    A file whose body matches and whose header is exactly what this writes (any timestamp) is
    left alone, since every rewrite would be one more sync commit. Anything else in the
    header, such as a hand edit, gets the whole file rewritten."""
    prof = profile()
    if prof is None:
        return None
    path = os.path.join(PUBLISH_DIR, f"{prof}.md")
    body = publish_body(items)
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read().split("\n", 3)
        title, generated, blank = published_header(prof, "\0")  # what this writes, any timestamp
        generated = re.escape(generated).replace("\0", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        if len(old) == 4 and (old[0], old[2], old[3]) == (title, blank, body) and re.fullmatch(generated, old[1]):
            return path
    except (OSError, ValueError):
        pass
    header = published_header(prof, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    os.makedirs(PUBLISH_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"  # gitignored (supervisors/*.tmp) in case cleanup never runs
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(header) + "\n" + body)
        os.replace(tmp, path)  # atomic: auto-sync never stages a half-written file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--profile", required=True,
                                            choices=[n for n in profile_names() if n != TEMPLATE_PROFILE],
                                            help="a profile with a profiles/<name>.md file (copy profiles/example.md first)")
    s = sub.add_parser("add"); s.add_argument("--title", required=True); s.add_argument("--objective", required=True)
    s.add_argument("--criteria", nargs="*", default=[]); s.add_argument("--owner"); s.add_argument("--repo")
    s = sub.add_parser("set"); s.add_argument("id"); s.add_argument("--state", choices=STATES); s.add_argument("--owner")
    s.add_argument("--drop-owner", help="release a session (reassignment, or a worker that ended)")
    s.add_argument("--evidence"); s.add_argument("--next"); s.add_argument("--note")
    s = sub.add_parser("decide"); s.add_argument("id"); s.add_argument("--question", required=True); s.add_argument("--recommendation", required=True)
    s.add_argument("--kind", required=True, choices=DECISION_KINDS,
                   help="why only the user can answer: " + "; ".join(f"{k} = {v}" for k, v in DECISION_KINDS.items()))
    s.add_argument("--tried", required=True,
                   help="what was probed to settle it without the user (deployment, logs, Slack, code, a live test, a check run) "
                        "and why that did not; for outbound, the draft. Stays local, never published")
    s = sub.add_parser("resolve"); s.add_argument("id"); s.add_argument("--answer", required=True)
    s = sub.add_parser("show"); s.add_argument("--all", action="store_true")
    sub.add_parser("owned"); sub.add_parser("render")
    a = p.parse_args()

    if a.cmd == "init":
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "profile"), "w") as f:
            f.write(a.profile + "\n")
        os.makedirs(SUP_HOME, exist_ok=True)
        for name in ("CLAUDE.md", "AGENTS.md"):
            with open(os.path.join(SUP_HOME, name), "w") as f:
                f.write(LAUNCHER.format(profile=a.profile))
        print(f"supervisor installed: state {STATE_DIR}, launcher {SUP_HOME}, profile {a.profile}")
        return

    # render too: an unlocked render could fold an older log and publish it after a newer write.
    with write_lock() if a.cmd in ("add", "set", "decide", "resolve", "render") else contextlib.nullcontext():
        run(a)


def run(a):
    items = fold()
    if a.cmd == "add":
        iid = next_id(items)
        append({"op": "add", "id": iid, "title": a.title, "objective": a.objective,
                "criteria": a.criteria, "owner": a.owner, "repo": a.repo})
        print(iid, flush=True)  # before render: the item exists even if publishing fails
        render(fold())
    elif a.cmd in ("set", "decide", "resolve"):
        if a.id not in items:
            sys.exit(f"no such item: {a.id}")
        if a.cmd == "set":
            if a.state in TERMINAL and open_decisions(items[a.id]):
                sys.exit(f"{a.id} has an open decision; resolve it before closing the item")
            append({"op": "set", "id": a.id, "state": a.state, "owner": a.owner, "drop_owner": a.drop_owner,
                    "evidence": a.evidence, "next": a.next, "note": a.note})
        elif a.cmd == "decide":
            if len(a.tried.strip()) < TRIED_MIN:
                sys.exit(f"--tried must say what was probed and why it did not settle this (at least {TRIED_MIN} "
                         "chars). Nothing tried: send the question back to the worker to probe first.")
            append({"op": "decide", "id": a.id, "question": a.question, "recommendation": a.recommendation,
                    "kind": a.kind, "tried": a.tried})
        else:
            if not open_decisions(items[a.id]):
                sys.exit(f"{a.id} has no open decision")
            append({"op": "resolve", "id": a.id, "answer": a.answer})
        render(fold())
    elif a.cmd == "show":
        rows = [it for it in items.values() if a.all or it["state"] not in TERMINAL or open_decisions(it)]
        print("\n".join(describe(it) for it in sorted(rows, key=lambda i: -i["updated"])) or "ledger: nothing in flight")
    elif a.cmd == "owned":
        for sid in owned_sessions(items):
            print(sid)
    elif a.cmd == "render":
        print(render(items))


if __name__ == "__main__":
    main()
