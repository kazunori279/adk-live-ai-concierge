"""What a tool argument looks like on its way to the journal.

Here rather than in `main.py` because two places need it and one of them
cannot import the other: `main.py:66` imports `plan`, so `plan.py` reaching
back for `main._sized` would close the cycle. Nothing in this module imports
anything of the package, which is what lets both sides have it.

See `main._log_tool_traffic` for why the log is sized at all, and #206.
"""

# The argument names worth printing whole: a language tag, a travel mode, and
# the two-or-three-word handle a fact is filed under. Each is a short closed
# vocabulary the model picks from rather than anything the household said, and
# `memory.py:406` already logs the topic for that reason. Everything else is
# sized instead — see `_log_tool_traffic`.
_PLAIN_ARGS = frozenset({"lang", "topic", "travel_mode"})


def _sized(value):
    """One tool argument as it goes to the log: how big it is, not what it is.

    A number, a flag or a missing value is not somebody's words and is worth
    more printed than counted — `limit=5` says something and `<1 chars>` does
    not. Anything else is measured if it can be and named if it cannot.
    """
    if isinstance(value, str):
        return f"<{len(value)} chars>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"
