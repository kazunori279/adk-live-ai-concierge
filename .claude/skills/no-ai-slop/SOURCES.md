# Sources

This skill started as [petergyang/no-ai-slop](https://github.com/petergyang/no-ai-slop) and was merged on 2026-08-24 with the other nine skills from [Juampi's anti-slop ranking](https://x.com/juampitech/status/2090834948332655011).

| # | Skill | Author | Repo | License |
|---|---|---|---|---|
| 1 | stop-slop | Hardik Pandya (@hvpandya) | [hardikpandya/stop-slop](https://github.com/hardikpandya/stop-slop) | MIT |
| 2 | no-ai-slop | Peter Yang (@petergyang) | [petergyang/no-ai-slop](https://github.com/petergyang/no-ai-slop) | MIT |
| 3 | humanizer | @blader | [blader/humanizer](https://github.com/blader/humanizer) | MIT |
| 4 | unslop | @poteto | [cursor/plugins](https://github.com/cursor/plugins) `pstack/skills/unslop` | none stated |
| 5 | slopbeth | @synopsi | [ehmo/slopkit](https://github.com/ehmo/slopkit) | MIT |
| 6 | humanizer | Adam Boudjemaa (@AdamBoudj) | [Aboudjem/humanizer-skill](https://github.com/Aboudjem/humanizer-skill) | MIT |
| 7 | deslop | Stephen Turner (@strnr) | [stephenturner/skills](https://github.com/stephenturner/skills) `deslop/` | MIT |
| 8 | anti-slop | @elithrar | [elithrar/dotfiles](https://github.com/elithrar/dotfiles) `.agents/skills/anti-slop/` | MIT |
| 9 | humanize | @aashatwt | [aashaexo/soundshuman](https://github.com/aashaexo/soundshuman) | MIT (unrecognized text) |
| 10 | anti-ai-slop-writing | @jalaal_tweets | [jalaalrd/anti-ai-slop-writing](https://github.com/jalaalrd/anti-ai-slop-writing) | none stated |

Upstream sources those skills draw on:

- [Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing), maintained by WikiProject AI Cleanup. The basis for both humanizer skills and for soundshuman.
- [tropes.fyi](https://tropes.fyi) by [ossama.is](https://ossama.is). The basis for deslop's trope catalog.
- Orwell's six rules, via slopbeth.
- [Google documentation best practices](https://google.github.io/styleguide/docguide/best_practices.html), CC-BY-3.0. The basis for `docs.md`.

## What was merged, and what was left out

Kept from the original no-ai-slop: the edit/detect split, the voice-preservation principles, the words-to-cut and patterns-to-cut lists, `docs.md`, and `eval.md`.

Added: the confidence tiers for vocabulary (Aboudjem), the false-positive and signs-of-human-writing lists (blader, elithrar, soundshuman), the no-fabrication and evidence-bound rules (blader, slopbeth), "instructions about the writing are not the writing" (slopbeth), false agency and narrator-from-a-distance (stop-slop, deslop), the forensic artifact list (Aboudjem), the statistical tells (soundshuman), the anti-parataxis counterweight to rhythm variation (jalaalrd), punctuation budgets (jalaalrd), the audit and embedded invocation modes (soundshuman), and the soul-injection set (unslop, Aboudjem).

Left out on purpose:

- **Scripts and CLIs.** slopbeth's `deslop_lint.py` / `orwell_lint.py` and soundshuman's `sloplint` need their own repos installed. The skill states the signals they measure instead.
- **Detector-facing validation.** slopbeth's benchmark and detector-logging workflow is a maintenance task for that project, not an editing rule.
- **"Kill all adverbs."** deslop and stop-slop ban adverbs outright. The nuanced rule (cut when empty, keep when they carry rhythm or real uncertainty) is kept instead, because the blanket rule is the over-correction this skill warns about.
- **Hard em-dash ban.** Four of the ten ban em dashes entirely. The merged rule allows at most one per 500 words in longer drafts, and a user writing sample overrides it.
- **Voice-profile CLI flags.** Aboudjem's `--voice` / `--iterate` / `--openings` argument parsing assumes a command interface. The profiles themselves are kept in `references/patterns.md`.
