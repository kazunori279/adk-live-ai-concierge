---
name: gws-calendar-agenda
version: 1.0.0
description: "Google Calendar: Show upcoming events across all calendars."
metadata:
  openclaw:
    category: "productivity"
    requires:
      bins: ["gws"]
    cliHelp: "gws calendar +agenda --help"
---

# calendar +agenda

> **PREREQUISITE:** Read `../gws-shared/SKILL.md` for auth, global flags, and security rules. If missing, run `gws generate-skills` to create it.

> **HAND-PATCHED — do not overwrite from `gws generate-skills`.** The generated
> text of this file described `--timezone` as working and showed `--format
> table`, and both cost the sub-agent a wasted round trip: the flag is accepted
> and ignored, and the output is a table whichever format you ask for. The
> corrections are marked below. A regenerate restores the original wording, so
> re-apply them or the calls come back. See #12.

Show upcoming events across all calendars

## Usage

```bash
gws calendar +agenda
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--today` | — | — | Show today's events |
| `--tomorrow` | — | — | Show tomorrow's events |
| `--week` | — | — | Show this week's events |
| `--days` | — | — | Number of days ahead to show |
| `--calendar` | — | — | Filter to specific calendar name or ID |
| `--timezone` | — | — | *(patched)* Accepted and ignored. Times come back in the Google account timezone whatever you pass. |

## Examples

```bash
gws calendar +agenda
gws calendar +agenda --today
gws calendar +agenda --week --format json
gws calendar +agenda --days 3 --calendar 'Work'
```

## Tips

- Read-only — never modifies events.
- Queries all calendars by default; use --calendar to filter.
- *(patched)* Times are always in the Google account timezone. `--timezone`
  does not change them, and neither does anything else.
- *(patched)* Output is a table even with `--format json`. Parse it as text, or
  use `gws calendar events list`, which does honour the flag.

## See Also

- [gws-shared](../gws-shared/SKILL.md) — Global flags and auth
- [gws-calendar](../gws-calendar/SKILL.md) — All manage calendars and events commands
