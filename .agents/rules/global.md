# Global Rules

> Copied from `~/.claude/CLAUDE.md` on 2026-09-11. It used to be a symlink to
> that absolute path, which recorded a broken link in every checkout but this
> one. **The two are no longer kept in sync by the filesystem** — edit
> `~/.claude/CLAUDE.md` and this file has to be updated by hand, or the coder
> session silently stops following the rule that changed.

- Answer in Japanese. I write my prompts in English because it is easier to
  type, but all prose you write back to me — reports, explanations,
  recommendations, ordinary replies — must be in Japanese. Keep it concise and
  easy to follow for a developer; no verbose padding. Stay in English: code,
  identifiers, commit messages, and repo file contents (README etc.). Keep
  technical terms as-is instead of forcing them into katakana.
- Do not mention Claude or add Co-Authored-By lines referencing Claude in commit messages.
- Use `uv` over `pip` when possible. For new projects, create a `pyproject.toml` with dependencies and use `uv sync` / `uv run` instead of manually managing a venv.

## Startup: load this skill

At the start of every session, before doing other work, invoke the `no-ai-slop`
skill via the Skill tool so its guidance is active. It applies to any prose you
write: replies, reports, commit messages, READMEs, docs, and comments — not only
to drafts I hand you for editing. When the writing is technical documentation,
also follow `docs.md` in that skill.
