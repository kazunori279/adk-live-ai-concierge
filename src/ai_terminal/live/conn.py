"""Which connection a piece of tool work belongs to.

A session id stopped being one per connection at #19 — it is one per profile,
so the kiosk in the kitchen and a browser tab on the Mac that serves it hold
the same id at the same time, indefinitely. #127 taught ``screen._SINKS`` to
survive that by keeping a stack. The three tool registries beside it kept one
entry per session id, which means the second connection's ``open_session``
threw the first's entry away and the second's ``close_session`` then took the
survivor's place in the dictionary with it.

The registries here are two levels — session id, then connection id — so every
connection has an entry of its own and a close only ever removes one. What that
buys over a stack is the second half of the problem: a lookup can answer with
*the caller's own* entry rather than the newest, so a browse the kiosk started
is cancelled when the kiosk leaves and not when somebody else's tab does.

Naming the caller is the part ``ToolContext`` cannot help with. It carries the
session, the invocation and the function call (`google/adk/agents/context.py`
and `readonly_context.py`, google-adk 2.8.0) and nothing that distinguishes two
sockets on one session. So the connection names itself the same way the live
session already does across the ``plan`` boundary — a ContextVar, snapshotted
into every task created under it, which is what `screen.live` and
`_gws_run.budget` are built on. :func:`own` falls back to the newest entry when
nothing has been named, so a caller the var never reached behaves exactly as a
stack would rather than losing its registry entry.
"""

import contextlib
import contextvars
import itertools
from typing import Any, TypeVar

# The connection everything running under this task belongs to, or None when
# nothing has said. A ContextVar and not an argument for the reason given in
# `screen.live`: ADK calls a tool with the model's arguments and nothing else,
# so there is no parameter between a socket and the tool it ends up running.
_CONN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ai_terminal_connection", default=None
)

_COUNT = itertools.count(1)

T = TypeVar("T")

# session id -> connection id -> whatever that connection registered
Registry = dict[str, dict[str, Any]]


def new_id() -> str:
    """An id for a connection that has not been given one.

    Distinct per call, so two anonymous opens on one session id are two
    connections rather than one overwriting the other — which is the whole
    failure this module exists for, and it should not come back by way of a
    default.
    """
    return f"c{next(_COUNT)}"


def arrive() -> str:
    """Mint an id for this connection and name it for everything below.

    Set rather than scoped, because the scope wanted is the WebSocket handler's
    own task and that is exactly the task this runs in: it ends when the
    connection does, and every task created from it — the live run, the tool
    calls ADK starts inside it, the sub-runs under `plan` — takes a snapshot of
    the context on the way out. Use :func:`speaking` where a block is wanted.
    """
    conn_id = new_id()
    _CONN.set(conn_id)
    return conn_id


def current() -> str | None:
    """The connection this code is running for, or None if nobody said."""
    return _CONN.get()


@contextlib.contextmanager
def speaking(conn_id: str | None):
    """Name the connection for the length of a block, then put it back."""
    token = _CONN.set(conn_id)
    try:
        yield
    finally:
        _CONN.reset(token)


def opened(registry: Registry, session_id: str, entry: T,
           conn_id: str | None = None) -> T:
    """File this connection's entry under the session, beside any others.

    The entry is handed straight back because it doubles as the token
    :func:`taken` matches on — the same trick the three ``open_session``
    functions already used, with no second dictionary to keep in step.
    """
    registry.setdefault(session_id, {})[conn_id or new_id()] = entry
    return entry


def own(registry: Registry, session_id: str) -> Any | None:
    """The entry belonging to the connection asking, or the newest there is.

    The caller's own where it can be found, because these registries hold work
    that is cancelled when a connection drops: a browse job or a sub-agent run
    filed under somebody else's tab dies when that tab closes, on a terminal
    that is still up and still waiting for the answer.

    Newest otherwise, which is the same rule `screen.sink` settled on and the
    right one to fall back to: an unnamed caller is a caller with no connection
    of its own to prefer, and the alternative — answering None — is the refusal
    this whole module is here to stop.
    """
    entries = registry.get(session_id)
    if not entries:
        return None
    conn_id = current()
    if conn_id is not None and conn_id in entries:
        return entries[conn_id]
    return next(reversed(entries.values()))


def taken(registry: Registry, session_id: str,
          entry: Any | None = None) -> list:
    """Remove what this close owns and return it, for the caller to cancel.

    `entry` is the token :func:`opened` handed back, and identity is the test,
    not equality: two connections' registries are separate objects that compare
    equal while they are both empty, and a close must not be able to take the
    wrong one. A token that is already gone matches nothing and comes back as
    an empty list — the caller still cancels its own work, which never depended
    on the registry.

    Omitted, `entry` means every connection on the session, which is what a
    caller with only an id to go on means.

    The session key goes when the last connection does, so ``session_id in
    registry`` still reads as "somebody is connected" — which is what
    ``gws.close_session`` hangs the `_ASKING` clear on.
    """
    entries = registry.get(session_id)
    if entries is None:
        return []
    if entry is None:
        registry.pop(session_id, None)
        return list(entries.values())
    gone = []
    for conn_id, held in list(entries.items()):
        if held is entry:
            del entries[conn_id]
            gone.append(held)
            break
    if not entries:
        registry.pop(session_id, None)
    return gone
