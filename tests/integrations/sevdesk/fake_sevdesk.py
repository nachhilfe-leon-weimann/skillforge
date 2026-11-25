"""A synthetic sevDesk behind ``httpx.MockTransport``: canned answers per route, every request remembered.

It also answers from fixtures: ``recorded`` reads SP-SD's run (never edited), ``documented`` the files derived from
sevDesk's documentation (``tests/fixtures/sevdesk/documented/README.md``).
"""

import json
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import httpx

TOKEN = "synthetic-sevdesk-token-for-tests"
ACCOUNT_ID = 4711
TRIAL_COMPANY = "Example Tutoring TRIAL"

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sevdesk"
RECORDED_RUN = "run202611112124164167"
"""SP-SD's one recorded run (billing spec, "Fixture manifest")."""

type Answer = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def objects(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"objects": payload})


def page(items: Sequence[Mapping[str, object]], total: int | None = None) -> httpx.Response:
    """A page of a ``countAll`` listing; ``total`` defaults to the page's own length, as a string like sevDesk's."""
    return httpx.Response(200, json={"total": str(len(items) if total is None else total), "objects": list(items)})


@cache
def _fixture(path: Path) -> str:
    return path.read_text()


def recorded(name: str) -> dict[str, Any]:
    """A fixture of the recorded run, as a fresh copy: a test may change it, the file never."""
    return json.loads(_fixture(FIXTURES / RECORDED_RUN / f"{name}.json"))


def documented(name: str) -> dict[str, Any]:
    """A fixture derived from sevDesk's documentation, as a fresh copy."""
    return json.loads(_fixture(FIXTURES / "documented" / f"{name}.json"))


def recorded_object(name: str, object_id: int | None = None) -> dict[str, Any]:
    """The object a recorded read answered with: the only one, or the one with ``object_id`` from a listing."""
    found = recorded(name)["response"]["body"]["objects"]
    if object_id is None:
        [found] = found
        return found
    [match] = [item for item in found if item["id"] == str(object_id)]
    return match


def invoice_listing(by_number: Mapping[str, Sequence[Mapping[str, object]]]) -> Answer:
    """``GET /Invoice`` filtered by ``invoiceNumber`` as documented: the documented envelope, holding the invoices
    listed for the number asked - none for any other. A request without the documented filter is refused."""
    shape = documented("invoice-by-number")
    filter_name = next(iter(shape["request"]["params"]))

    def answer(request: httpx.Request) -> httpx.Response:
        if filter_name not in request.url.params:
            return httpx.Response(400, json={"error": {"message": "synthetic: no invoiceNumber filter"}})
        listed = list(by_number.get(request.url.params[filter_name], ()))
        body = {**shape["response"]["body"], "total": str(len(listed)), "objects": listed}
        return httpx.Response(shape["response"]["status"], json=body, headers=shape["response"]["headers"])

    return answer


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
