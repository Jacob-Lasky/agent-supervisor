#!/usr/bin/env python3
"""Install the supervisor package into ~/.claude, safely and idempotently.

  python3 install.py [--dry-run] [--claude-dir DIR]

- Copies skills/{supervisor,hypervisor,catchup}, hooks/*.sh and hooks/tests/* into ~/.claude.
- Merges the hook wiring from config/settings.snippet.json into ~/.claude/settings.json:
  each entry is appended to that event's array unless the same command is already wired there,
  so running it twice changes nothing. Every other setting is left exactly as it was.
- Before overwriting any existing file that differs (a skill file, a hook, settings.json), copies
  it to ~/.claude/backups/agent-supervisor-<UTC timestamp>/ at the same relative path.
- Never touches profiles you have written (profiles/<name>.md other than example.md), the
  supervisor state directory, or anything outside the paths above.

Standard library only. Prints every change; --dry-run prints them and writes nothing.
"""
import argparse
import filecmp
import json
import os
import shutil
import sys
import time

PKG = os.path.dirname(os.path.abspath(__file__))
SKILLS = ("supervisor", "hypervisor", "catchup")
SNIPPET = os.path.join(PKG, "config", "settings.snippet.json")


def package_files():
    """(source path, path relative to ~/.claude) for everything the package installs."""
    out = []
    for skill in SKILLS:
        root = os.path.join(PKG, "skills", skill)
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith((".pyc", ".orig")):
                    src = os.path.join(d, f)
                    out.append((src, os.path.relpath(src, PKG)))
    for sub in ("hooks", os.path.join("hooks", "tests")):
        d = os.path.join(PKG, sub)
        for f in sorted(os.listdir(d)):
            src = os.path.join(d, f)
            if os.path.isfile(src):
                out.append((src, os.path.join(sub, f)))
    return out


def merge_hooks(settings, snippet):
    """Return (merged settings, [(event, command)] added). Pure: does not touch the inputs."""
    merged = json.loads(json.dumps(settings))
    hooks = merged.setdefault("hooks", {})
    added = []
    for event, entries in snippet["hooks"].items():
        have = hooks.setdefault(event, [])
        wired = {h.get("command") for e in have for h in e.get("hooks", []) if isinstance(h, dict)}
        for entry in entries:
            cmds = [h["command"] for h in entry["hooks"]]
            if all(c in wired for c in cmds):
                continue
            have.append(json.loads(json.dumps(entry)))
            added += [(event, c) for c in cmds]
    return merged, added


def install(claude_dir, dry_run=False, now=None):
    """Install into claude_dir. Returns a list of human-readable change lines."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    backup_root = os.path.join(claude_dir, "backups", f"agent-supervisor-{stamp}")
    changes = []

    def backup(rel):
        dst = os.path.join(backup_root, rel)
        changes.append(f"backup   {rel} -> {os.path.relpath(dst, claude_dir)}")
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(claude_dir, rel), dst)

    for src, rel in package_files():
        dst = os.path.join(claude_dir, rel)
        if os.path.exists(dst):
            if filecmp.cmp(src, dst, shallow=False):
                continue
            backup(rel)
            changes.append(f"update   {rel}")
        else:
            changes.append(f"add      {rel}")
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            if rel.startswith("hooks") and rel.endswith(".sh"):
                os.chmod(dst, 0o755)

    path = os.path.join(claude_dir, "settings.json")
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON ({e}); fix it first, nothing was changed to it")
    with open(SNIPPET) as f:
        snippet = json.load(f)
    merged, added = merge_hooks(settings, snippet)
    for event, cmd in added:
        changes.append(f"wire     {event}: {cmd}")
    if added:
        if os.path.exists(path):
            backup("settings.json")
        if not dry_run:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(merged, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
    return changes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    p.add_argument("--claude-dir", default=os.path.join(os.path.expanduser("~"), ".claude"))
    a = p.parse_args()
    changes = install(a.claude_dir, a.dry_run)
    print("\n".join(changes) if changes else "already installed: nothing to change")
    if changes and not a.dry_run:
        print("Installed. Skills load in the NEXT Claude Code session, not this one.")


if __name__ == "__main__":
    sys.exit(main())
