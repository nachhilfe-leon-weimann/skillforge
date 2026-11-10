"""A synthetic sevDesk behind ``httpx.MockTransport``: canned answers per route, every request remembered."""

from collections.abc import Callable, Mapping, Sequence

import httpx

TOKEN = "synthetic-sevdesk-token-for-tests"
ACCOUNT_ID = 4711
TRIAL_COMPANY = "Example Tutoring TRIAL"

type Answer = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def objects(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"objects": payload})


def page(items: Sequence[Mapping[str, object]], total: int | None = None) -> httpx.Response:
    """A page of a ``countAll`` listing; ``total`` defaults to the page's own length, as a string like sevDesk's."""
    return httpx.Response(200, json={"total": str(len(items) if total is None else total), "objects": list(items)})


class FakeSevdesk:
    """Answers ``(method, path)`` routes below ``/api/v1``; an unknown route is a 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Answer] = {
            ("GET", "/SevClient"): objects([{"id": str(ACCOUNT_ID), "objectName": "SevClient", "name": TRIAL_COMPANY}]),
        }

    def on(self, method: str, path: str, answer: Answer) -> None:
        self.routes[(method, path)] = answer

    def sent(self, method: str | None = None) -> list[str]:
        return [
            f"{request.method} {request.url.path.removeprefix('/api/v1')}"
            for request in self.requests
            if method is None or request.method == method
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path.removeprefix("/api/v1")))
        match answer:
            case None:
                return httpx.Response(404, json={"error": {"message": "Not found"}})
            case Exception():
                raise answer
            case httpx.Response():
                return answer
            case _:
                return answer(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
