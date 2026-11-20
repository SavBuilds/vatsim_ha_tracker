#!/usr/bin/env python3
"""Pre-release scan for personal data.

Usage:
    python tools/pii_scan.py [--seeds FILE] [--allow FILE] [--no-git] PATH [PATH ...]

Two sources of patterns:
  1. Built-in generic patterns (tokens, private addresses, emails, entity ids
     that only exist in one house, precise coordinates). These hold no personal
     values and ship with the repo.
  2. A seed file that you keep out of git (default tools/pii_seeds.local.txt).
     One seed per line:  category<TAB>value
     A value starting with "re:" is a regular expression; "cs:" forces a
     case-sensitive literal; anything else is a case-insensitive literal.

An allow file (default tools/pii_allow.txt, shipped) accepts known-good hits
for the built-in patterns. Rules that name one of your seeds go in
tools/pii_allow.local.txt next to it (gitignored, loaded automatically), so a
shipped file never reveals what your seeds are.
One rule per line:  path-glob<TAB>seed-or-pattern-name<TAB>regex the line must match
A hit is dropped only when all three match.

If a scanned path is a git work tree, every blob in every commit is scanned
too (git cat-file), so a value removed from the tree but still in history is
reported. Exit status: 0 clean, 1 findings, 2 usage error.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys

BUILTIN = [
    ("credential", "jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("credential", "bearer", r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    ("credential", "long-hex", r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{32,}(?![A-Fa-f0-9])"),
    ("credential", "long-base64", r"(?<![A-Za-z0-9+/=_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])[A-Za-z0-9+/_-]{40,}={0,2}"),
    ("credential", "key-assignment", r"(?i)\b(api_key|password|passwd|token|secret)\s*[:=]\s*[\"']?[A-Za-z0-9._/+-]{6,}"),
    ("network", "rfc1918", r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])"),
    ("network", "cgnat-100.64", r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d.])"),
    ("network", "ts.net", r"[A-Za-z0-9-]+\.ts\.net"),
    ("network", "mac", r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"),
    ("network", "host-with-port", r"(?i)\b(?!homeassistant:)[a-z0-9][a-z0-9.-]*:8123\b"),
    ("account", "email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("path", "windows-user", r"(?i)[A-Z]:\\Users\\[^\\\s\"']+"),
    ("path", "unix-home", r"/(?:home|Users)/[A-Za-z0-9._-]+"),
    ("ha-id", "media_player", r"\bmedia_player\.[a-z0-9_]+"),
    ("ha-id", "notify-target", r"\bnotify\.[a-z0-9_]+"),
    ("ha-id", "mobile_app", r"\bmobile_app_[a-z0-9_]+"),
    ("ha-id", "person", r"\bperson\.[a-z0-9_]+"),
    ("location", "precise-latlon", r"(?<![\d.])-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}(?![\d.])"),
    ("location", "latlon-key", r"(?i)\b(lat|latitude|lon|lng|longitude)\b[\"']?\s*[:=]\s*-?\d{1,3}\.\d{3,}"),
]

TEXT_EXT_SKIP = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pyc", ".zip", ".gz", ".dat"}


def load_seeds(path):
    seeds = []
    if not path or not os.path.exists(path):
        return seeds
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" not in line:
                sys.exit(f"{path}:{n}: expected category<TAB>value")
            cat, val = line.split("\t", 1)
            if val.startswith("re:"):
                rx = re.compile(val[3:])
            elif val.startswith("cs:"):
                rx = re.compile(re.escape(val[3:]))
            else:
                rx = re.compile(re.escape(val), re.IGNORECASE)
            seeds.append((cat.strip(), val, rx))
    return seeds


def load_allow(path):
    rules = []
    if not path or not os.path.exists(path):
        return rules
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                sys.exit(f"{path}:{n}: expected glob<TAB>name<TAB>line-regex")
            rules.append((parts[0], parts[1], re.compile(parts[2])))
    return rules


def allowed(rules, relpath, name, line):
    rel = relpath.replace("\\", "/")
    for glob, rule_name, line_rx in rules:
        if rule_name == name and fnmatch.fnmatch(rel, glob) and line_rx.search(line):
            return True
    return False


def scan_text(label, text, patterns, rules, out):
    for lineno, line in enumerate(text.splitlines(), 1):
        for cat, name, rx in patterns:
            m = rx.search(line)
            if m and not allowed(rules, label.split("@", 1)[0], name, line):
                out.append((label, lineno, cat, name, m.group(0), line.strip()[:140]))


def iter_files(root):
    if os.path.isfile(root):
        yield root, os.path.basename(root)
        return
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in sorted(files):
            full = os.path.join(base, f)
            yield full, os.path.relpath(full, root)


def commit_text(raw):
    """Author, committer and message of a commit or tag object. The tree and
    parent hashes and any signature block are not personal data and would
    only trip the long-hex and base64 patterns."""
    header, _, message = raw.partition("\n\n")
    who = [line for line in header.split("\n") if line.startswith(("author ", "committer ", "tagger "))]
    return "\n".join(who) + "\n\n" + message


def scan_git(root, patterns, rules, out, skip_names=()):
    if not os.path.isdir(os.path.join(root, ".git")):
        return 0
    try:
        listing = subprocess.run(
            ["git", "-C", root, "rev-list", "--all", "--objects"],
            capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"WARNING: could not read git objects in {root}: {exc}", file=sys.stderr)
        return 0
    seen = 0
    for entry in listing:
        sha, _, name = entry.partition(" ")
        kind = subprocess.run(["git", "-C", root, "cat-file", "-t", sha],
                              capture_output=True, text=True).stdout.strip()
        if kind not in ("blob", "commit", "tag"):
            continue
        data = subprocess.run(["git", "-C", root, "cat-file", "-p", sha], capture_output=True).stdout
        if b"\0" in data[:4096] or name in skip_names:
            continue
        seen += 1
        text = data.decode("utf-8", "replace")
        if kind != "blob":
            text = commit_text(text)
        # A tag object is listed with its tag name; label it "commit" so the
        # same allow rules cover author, committer and tagger lines.
        label = name if kind == "blob" else "commit"
        scan_text(f"{label}@git:{sha[:10]}", text, patterns, rules, out)
    return seen


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Scan a release tree for personal data.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--seeds", default=os.path.join(here, "pii_seeds.local.txt"))
    ap.add_argument("--allow", default=os.path.join(here, "pii_allow.txt"))
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--require-seeds", action="store_true",
                    help="fail if the seed file is missing (use for a real release run)")
    args = ap.parse_args()

    seeds = load_seeds(args.seeds)
    if args.require_seeds and not seeds:
        print(f"ERROR: no seeds loaded from {args.seeds}", file=sys.stderr)
        return 2
    local_allow = os.path.splitext(args.allow)[0] + ".local.txt"
    rules = load_allow(args.allow) + load_allow(local_allow)
    patterns = [(c, n, re.compile(p)) for c, n, p in BUILTIN] + seeds
    skip = {os.path.abspath(p) for p in (args.seeds, args.allow, local_allow)}

    out, files, blobs = [], 0, 0
    for root in args.paths:
        for full, rel in iter_files(root):
            if os.path.abspath(full) in skip:
                continue
            if os.path.splitext(full)[1].lower() in TEXT_EXT_SKIP:
                print(f"NOTE: not scanned (binary or data file, inspect by hand): {rel}", file=sys.stderr)
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            if b"\0" in data[:4096]:
                print(f"NOTE: not scanned (binary, inspect by hand): {rel}", file=sys.stderr)
                continue
            files += 1
            scan_text(rel, data.decode("utf-8", "replace"), patterns, rules, out)
        if not args.no_git and os.path.isdir(root):
            names = {os.path.relpath(p, root).replace(os.sep, "/") for p in skip}
            blobs += scan_git(root, patterns, rules, out, names)

    for label, lineno, cat, name, match, line in out:
        print(f"{label}:{lineno}, {cat}, {name}, matched {match!r}: {line}")
    print(f"\n{len(out)} finding(s) in {files} file(s) and {blobs} git object(s); "
          f"{len(seeds)} seed(s), {len(rules)} allow rule(s).", file=sys.stderr)
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
