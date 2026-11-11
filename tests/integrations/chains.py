"""What an integration error carries with it: every exception chained to it, as cause or as (suppressed) context."""

import traceback

import httpx


def chained(error: BaseException) -> list[BaseException]:
    """The error and every exception reachable through ``__cause__`` and ``__context__``, ``from None`` included."""
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is seen for seen in found):
            continue
        found.append(current)
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
    return found


def carried_texts(error: BaseException) -> list[str]:
    """The formatted traceback and ``str``/``repr`` of every chained exception."""
    return ["".join(traceback.format_exception(error))] + [
        text for current in chained(error) for text in (str(current), repr(current))
    ]


def carries_a_request(error: BaseException) -> bool:
    """Whether any chained exception is an httpx error or holds an ``httpx.Request`` - with its headers and body."""
    return any(
        isinstance(current, httpx.HTTPError)
        or any(isinstance(value, httpx.Request) for value in vars(current).values())
        for current in chained(error)
    )
