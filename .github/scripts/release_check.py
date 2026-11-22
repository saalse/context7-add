#!/usr/bin/env python3
"""Check that the release version is consistent, and extract its release notes.

The version lives in three places that must agree: .claude-plugin/plugin.json ("version"),
skills/context7-add/SKILL.md (metadata.version) and a "## [X.Y.Z]" section in CHANGELOG.md.

  release_check.py                       check the three agree (CI runs this on every PR)
  release_check.py --tag vX.Y.Z          also check the tag matches the version
  release_check.py --notes-out FILE      write that version's CHANGELOG section to FILE

Exit codes: 0 consistent, 1 mismatch or missing section, 2 bad input.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def plugin_version():
    return json.loads(read(".claude-plugin/plugin.json"))["version"]


def skill_version():
    text = read("skills/context7-add/SKILL.md")
    front = text.split("---")[1] if text.startswith("---") else ""
    m = re.search(r'^\s+version:\s*"?([^"\n]+)"?\s*$', front, re.M)
    return m.group(1).strip() if m else None


def changelog_section(version):
    """The body of '## [version] ...' up to the next '## ' heading, or None."""
    text = read("CHANGELOG.md")
    m = re.search(r"^## \[" + re.escape(version) + r"\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.M | re.S)
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def check(tag=None):
    """Return (version, list of problems)."""
    problems = []
    pv, sv = plugin_version(), skill_version()
    if not SEMVER.match(pv or ""):
        problems.append(f"plugin.json version {pv!r} is not X.Y.Z")
    if sv != pv:
        problems.append(f"SKILL.md metadata.version {sv!r} differs from plugin.json {pv!r}")
    if changelog_section(pv) is None:
        problems.append(f"CHANGELOG.md has no non-empty '## [{pv}]' section")
    if tag is not None and tag != "v" + pv:
        problems.append(f"tag {tag!r} does not match version v{pv}")
    return pv, problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", help="the release tag, e.g. v1.2.0 (in Actions: $GITHUB_REF_NAME)")
    p.add_argument("--notes-out", metavar="FILE", help="write the version's CHANGELOG section to FILE")
    args = p.parse_args(argv)
    if args.tag is not None and not re.match(r"^v\d+\.\d+\.\d+$", args.tag):
        print(f"tag {args.tag!r} is not vX.Y.Z", file=sys.stderr)
        return 2
    version, problems = check(args.tag)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    if args.notes_out:
        with open(args.notes_out, "w", encoding="utf-8") as f:
            f.write(changelog_section(version) + "\n")
    print(json.dumps({"version": version, "tag": args.tag, "ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
