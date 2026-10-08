"""Check that stripping a title never gives a writing system more terms.

`written_in` takes its numerator from the stripped title and its denominator
from the raw one (`_match.py:45-47`), so the arithmetic only makes sense if the
strip can lower a bucket's count and never raise it. It has been raised twice.
Both times the mechanism was the same: `_units` counts a Latin run as one term
however long it is, and `_DECORATION`'s lookaround treats `-` and `'` as word
boundaries, so taking a tag out of the middle of a hyphenated word splits one
run into two and the Latin count goes *up*.

    plain("Ro-4k-tour") == "Ro- -tour"
    _units(raw)     {'LATIN': 1}
    _units(stripped) {'LATIN': 2}     <- more Latin than the title has

Two titles in a commit message do not settle that, which is why this is
exhaustive: every string of five atoms drawn from an alphabet picked to attack
the tag rule. The atoms are the tags themselves, the two characters the
lookaround treats as boundaries, both bracket pairs, all three separators, a
space, and one word of each script.

Length five, specifically. Narrow `_DECORATION`'s lookaround back to `-` alone,
which is the bug that was fixed, and this alphabet finds 3 violations at length
5 and **0 at length 4** — the shortest one that shows it is `Rokko'4k'Rokko`,
five atoms. A sweep that economised on length would run 65,536 cases and prove
nothing.

    uv run python tools/strip_invariant.py

Three seconds. Exits non-zero on the first violation, printing the title and
both counts.
"""

import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ai_terminal.live.tools import _match  # noqa: E402

ALPHABET = ["4k", "vlog", "hd", "【", "】", "[", "]", "-", "'", " ",
            "六甲山", "Rokko", "2024", "／", "｜", "/"]
LENGTH = 5


def main() -> None:
    plain, units = _match.plain, _match._units
    total = len(ALPHABET) ** LENGTH
    print(f"{len(ALPHABET)} atoms at length {LENGTH} = {total} titles")
    for parts in itertools.product(ALPHABET, repeat=LENGTH):
        title = "".join(parts)
        raw = units(title)
        stripped = units(plain(title))
        for bucket, count in stripped.items():
            if count > raw.get(bucket, 0):
                print(f"VIOLATION {title!r}\n"
                      f"  plain    {plain(title)!r}\n"
                      f"  raw      {raw}\n"
                      f"  stripped {stripped}")
                sys.exit(1)
    print(f"{total} titles, 0 violations")


if __name__ == "__main__":
    main()
