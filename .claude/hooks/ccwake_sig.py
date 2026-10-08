#!/usr/bin/env python3
"""Work signatures for ccwake.sh and ccloop.sh (#698).

A role's signature is a short hash of the GitHub state that is that role's work.
ccloop.sh records it at the end of every tick, which absorbs the role's own
writes; ccwake.sh recomputes it every poll and wakes the role only when the live
value differs from the recorded one. Waking on state rather than on events is
the point: every session writes as the same login, so an event cannot tell a
role's own write from anyone else's, and a role that woke on events would wake
itself.

    ccwake_sig.py --role reviewer        one line: the reviewer's signature
    ccwake_sig.py --all                  one line per role: "<role> <signature>"
    ccwake_sig.py --fixture f.json ...   read the GraphQL response from a file

The whole repository state comes from one GraphQL query, so one poll costs one
request whatever the number of roles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ROLES = ("coder", "reviewer", "tester")

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50) {
      nodes { number headRefOid comments(last: 1) { nodes { id } } }
    }
    triage: issues(states: OPEN, labels: ["stage:triage"], first: 100) { nodes { number } }
    ready: issues(states: OPEN, labels: ["stage:ready"], first: 100) { nodes { number } }
    live: issues(states: OPEN, labels: ["stage:live"], first: 100) {
      nodes { number comments(last: 1) { nodes { id } } }
    }
    closed: issues(states: CLOSED, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number labels(first: 20) { nodes { name } } }
    }
    main: ref(qualifiedName: "refs/heads/main") { target { oid } }
  }
}
"""


def fetch(repo: str) -> dict:
    owner, name = repo.split("/", 1)
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(out)


def _last_comment(node: dict) -> str:
    nodes = (node.get("comments") or {}).get("nodes") or []
    return nodes[-1]["id"] if nodes else "-"


def parts(data: dict) -> dict[str, list[str]]:
    """The raw state each role watches, before hashing. Tests read this."""
    repo = data["data"]["repository"]
    prs = repo["pullRequests"]["nodes"]
    unverified = [
        n["number"]
        for n in repo["closed"]["nodes"]
        if "stage:verified" not in {lab["name"] for lab in n["labels"]["nodes"]}
    ]
    main = ((repo.get("main") or {}).get("target") or {}).get("oid", "-")
    return {
        "coder": sorted(f"pr{p['number']}@{p['headRefOid']}" for p in prs),
        "reviewer": sorted(
            [f"triage{n['number']}" for n in repo["triage"]["nodes"]]
            + [f"ready{n['number']}" for n in repo["ready"]["nodes"]]
            + [f"pr{p['number']}@{p['headRefOid']}#{_last_comment(p)}" for p in prs]
            + [f"closed{n}" for n in unverified]
        ),
        "tester": [f"main@{main}"]
        + sorted(f"live{n['number']}#{_last_comment(n)}" for n in repo["live"]["nodes"]),
    }


def signatures(data: dict) -> dict[str, str]:
    return {
        role: hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]
        for role, items in parts(data).items()
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--role", choices=ROLES)
    group.add_argument("--all", action="store_true")
    ap.add_argument("--fixture", type=pathlib.Path)
    ap.add_argument("--repo", default="kazunori279/adk-live-ai-concierge")
    args = ap.parse_args()

    try:
        data = json.loads(args.fixture.read_text()) if args.fixture else fetch(args.repo)
        sigs = signatures(data)
    except Exception as exc:  # a failed poll must never look like a change
        print(f"ccwake_sig: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        for role in ROLES:
            print(role, sigs[role])
    else:
        print(sigs[args.role])


if __name__ == "__main__":
    main()
