"""Check that no model default has been left behind a name this repo already runs.

Six variables name a model, each an `os.getenv` with a default, and the
default is what every machine actually runs: no `.env` here, on the Pi or in
any unit sets one. So the defaults are the configuration. They drift the way
any six-place constant drifts — four moved to 3.x over a month while
`GROUNDING_MODEL` stayed on `gemini-2.5-flash-lite`, and what noticed was a
person reading the README, not a run.

The hard part is what to measure against, because the obvious answer is wrong.
`google-genai` ships the served model list as a `Literal` in
`_gaos/types/interactions/model.py`, and that list is the **Gemini API**
surface — its own docstring points at `ai.google.dev/gemini-api/docs/models`.
This project is on Vertex, and the search grounding POST is on Vertex in both
modes (see `SEARCH_ENDPOINT` in `_client.py`), so the two sets are not the
same one. Measured: that Literal has `gemini-3.1-flash-lite` and no
`gemini-3.5-flash-lite`, while the README's sub-agent benchmark ran three
medians on `gemini-3.5-flash-lite` on 2026-09-09 and wrote down what it
answered. Gating on the SDK list would have called a model this repo has
timed a typo. It is printed underneath as a report instead.

What can be decided from inside the tree is narrower and is the thing that
actually rotted: **a default is behind a name this repo itself runs.** Writing
a model id into the README, into a bench script or into `src/` is a claim that
the id exists — nobody publishes a latency table against a 404 — so the newest
id in a family is the newest one claimed, and a default in that family below
it is stale. `gemini-3.5-flash-lite` was in `gws_bench.py` and in two README
tables while `grounding_model` returned `gemini-2.5-flash-lite`, and that is
the whole of the defect, visible without leaving the checkout.

Family is the name with the version cut out of it, so `gemini-2.5-flash-lite`
and `gemini-3.5-flash-lite` are one family and `gemini-3.8-flash` is another.
Tier stays a decision: this says nothing about Flash against Flash-Lite, which
is what the README's benchmark settled and what nothing mechanical should
re-open. It says only that whichever tier was chosen, the newest one is run.

**Check scripts are not scanned, and that is not tidiness.**
`browse_check.py` sets `BROWSE_MODEL=gemini-4-flash` to prove `_thinking`
reads the major version rather than a list of names. It is a fixture for a
model that does not exist, and a rule that read it would demand every default
move to 4. Fixtures live in check scripts; claims live in prose and in the
scripts written to run one. `spikes/` is out for the reason `citations_check`
leaves it out — a vendored experiment is not this project saying anything.

The cost, stated: an id written into the README or a bench script that nobody
intends to default to turns this red. That is the toll, and it is the right
way round — the id is in the tree either way, and the question "why is the
default not this" is exactly the one that went a month unasked.

`LIVE_MODEL` is not compared this way. The Live API's names have their own
shape; ADK's fallback remains on 2.5, while this repo has moved to
`gemini-3.8-live`. The case below asserts `LIVE_MODEL` is on `gemini-3.8-live`.

Nothing here imports `ai_terminal`. The defaults are read out of the source
text, because reaching `_client.grounding_model` means importing
`ai_terminal.live.tools`, whose `__init__` builds the Workspace tool over the
`gws` CLI. A check on a string should not need an account.

    uv run python tools/models_check.py

Exits non-zero on the first case that does not hold. The SDK comparison is a
report underneath the count and does not gate.
"""

import ast
import re
import sys
from pathlib import Path

import google.adk
import google.genai

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


# ------------------------------------------------------- what is written down

# Every model default in `src/`, on one line each, which is how all six are
# written. One written in some other shape reads as absent, and the registry
# case below is what says so rather than the file quietly checking five.
DEFAULT = re.compile(r'os\.getenv\(\s*"(\w+_MODEL)"\s*,\s*"([^"]+)"\s*\)')

# The six, and the list is the point of it: a seventh variable added without a
# line here is a default nothing measures, which is the state this file exists
# to end.
EXPECTED = {
    "BROWSE_MODEL", "GROUNDING_MODEL", "GWS_MODEL", "HOME_MODEL",
    "LIVE_MODEL", "PLAN_MODEL", "SCREENSAVER_MODEL",
}

# Defaults the family rule cannot speak for, with the reason. `LIVE_MODEL` has
# a case of its own below; this is what keeps it out of the general rule
# rather than it being silently skipped by one.
EXEMPT = {
    "LIVE_MODEL": "a Live API name, which has no generation to be behind",
}

# Where a model id counts as claimed. README prose and the bench scripts,
# which exist to run one model against another, plus `src/` itself. Not
# `tools/*_check.py`: see the docstring on `gemini-4-flash`.
CLAIMED = ("README.md", "tools/*_bench.py", "src/**/*.py")

ANY_MODEL = re.compile(r"\b(gemini-[a-z0-9.\-]*[a-z0-9])\b")

# `gemini-3.8-flash` is version 3.8 of family `flash`; `gemini-3-pro-image` is
# version 3 of `pro-image`. A name with no version in that position —
# `gemini-flash-lite-latest`, `gemini-live-2.5-flash-native-audio` — does not
# parse and is not compared, which is right: an alias has no generation, and
# the live family numbers itself somewhere else in the name.
FAMILY = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(.+)$")


def defaults() -> dict[str, tuple[str, str]]:
    """Every `X_MODEL` default in `src/`, as name -> (value, where)."""
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for match in DEFAULT.finditer(line):
                found[match.group(1)] = (match.group(2),
                                         f"{path.relative_to(ROOT)}:{number}")
    return found


def claimed() -> dict[str, str]:
    """Every model id this repo writes down, as id -> the first place it is."""
    found: dict[str, str] = {}
    for pattern in CLAIMED:
        for path in sorted(ROOT.glob(pattern)):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for name in ANY_MODEL.findall(line):
                    found.setdefault(
                        name, f"{path.relative_to(ROOT)}:{number}")
    return found


def newest(family: str, names) -> str | None:
    """The highest-versioned name in one family, or None if it has none."""
    ranked = []
    for name in names:
        match = FAMILY.match(name)
        if match and match.group(2) == family:
            ranked.append(
                (tuple(int(p) for p in match.group(1).split(".")), name))
    return max(ranked)[1] if ranked else None


# ------------------------------------------------------------------- the cases


def registry_cases() -> None:
    print("\n== the six defaults, and that there are six ==")
    found = defaults()
    check("every model default in src/ is one this file knows about",
          sorted(found), sorted(EXPECTED))
    for name in sorted(found):
        value, where = found[name]
        check(f"{name} is read with a default", bool(value), True,
              f"{value} at {where}")


def family_cases() -> None:
    print("\n== and none of them is behind a name this repo runs ==")
    names = claimed()
    check("the sweep found the ids to compare against", len(names) >= 6, True,
          f"{len(names)} distinct ids across {', '.join(CLAIMED)}")
    for name, (value, where) in sorted(defaults().items()):
        if name in EXEMPT:
            print(f"   --   {name} is not compared: {EXEMPT[name]}")
            continue
        family = FAMILY.match(value)
        if not family:
            print(f"   --   {name} is {value}, which carries no version")
            continue
        top = newest(family.group(2), names)
        check(f"{name} is the newest {family.group(2)} written down here",
              top, value, f"{value} ({where})")


def live_cases() -> None:
    print("\n== the live model, against the library that builds the agent ==")
    value, where = defaults()["LIVE_MODEL"]
    source = (Path(google.adk.__file__).parent
              / "agents" / "llm_agent.py").read_text()
    adk = re.search(r"DEFAULT_LIVE_MODEL: ClassVar\[str\] = '([^']+)'", source)
    check("ADK still writes down a default live model", bool(adk), True)
    check("ADK default is gemini-live-2.5-flash-native-audio", adk.group(1), "gemini-live-2.5-flash-native-audio")
    check("and this repo is on gemini-3.8-live", value, "gemini-3.8-live", f"{value} ({where})")


def grounding_call_sites() -> list[tuple[str, str]]:
    """Find all generate_content calls using grounding_model(), as (where, client_name)."""
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                has_gm = any(
                    (kw.arg == "model" and isinstance(kw.value, ast.Call)
                     and getattr(kw.value.func, "id", None) == "grounding_model")
                    for kw in node.keywords
                )
                if has_gm and isinstance(node.func, ast.Attribute) and node.func.attr == "generate_content":
                    models_node = node.func.value
                    client_name = "unknown"
                    if isinstance(models_node, ast.Attribute) and models_node.attr == "models":
                        if isinstance(models_node.value, ast.Call) and isinstance(models_node.value.func, ast.Name):
                            client_name = models_node.value.func.id
                    sites.append((f"{path.relative_to(ROOT)}:{node.lineno}", client_name))
    return sites


def grounding_cases() -> None:
    print("\n== grounding model callers use global endpoints, not regional ==")
    sites = grounding_call_sites()
    check("grounding_model call sites were found in src/", len(sites) >= 4, True,
          f"{len(sites)} generate_content call sites")
    for where, client_name in sites:
        check(f"{where} does not use genai_client",
              client_name != "genai_client", True,
              f"uses {client_name}")
        check(f"{where} uses grounding_client or global_client",
              client_name in ("grounding_client", "global_client"), True,
              f"uses {client_name}")


def sdk_report() -> None:
    """What the installed SDK's own list says, printed and not gated on.

    Underneath the count, for the reason in the docstring: it is the Gemini
    API's set and this project is on Vertex, so a disagreement here is a
    question and not a verdict. Worth printing because the direction of the
    disagreement is informative — a default the Gemini API does not serve is
    one more thing to look at if the deployment ever moves off Vertex.
    """
    listed = re.findall(
        r'^\s*"([a-z0-9][a-z0-9.\-]*)",\s*$',
        (Path(google.genai.__file__).parent / "_gaos" / "types"
         / "interactions" / "model.py").read_text(), re.M)
    missing = [f"{name}={value}"
               for name, (value, _) in sorted(defaults().items())
               if value not in listed]
    print(f"\n   the installed google-genai lists {len(listed)} model ids, "
          "for the Gemini API rather than for Vertex")
    if missing:
        print("   not among them:")
        for line in missing:
            print(f"     {line}")


def main() -> None:
    registry_cases()
    family_cases()
    live_cases()
    grounding_cases()
    print(f"\nall {RAN} cases hold")
    sdk_report()


if __name__ == "__main__":
    main()
