"""Walk the sevDesk spike against the synthetic trial account: `just sevdesk-spike [--step STEP ...]`.

Only the maintainer runs it. It refuses unless ``SEVDESK__MODE=live``, ``SEVDESK__ACCOUNT=trial`` and the identity
check passes; the recorder keeps nothing before that, then records every exchange, starting with the identity read
again, into ``tests/fixtures/sevdesk/<run id>/``; a run that cannot record stops. Every object it creates carries
the run id - an invoice in ``customerInternalNote`` and its texts, a contact in its name. A write is never retried:
on an unknown outcome, an unreadable answer to a write included, the step stops and names the run tag to look for
in the trial. Its one ``DELETE`` targets a draft that carries this run's tag. It lives in ``scripts/``, which the
image does not contain, so it can never run from a deployed container.
"""

import argparse
import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IntegrationError, IntegrationRejectedError, OutcomeUnknownError
from app.integrations.recording import RUN_ID, Recorder, new_run_id
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.sevdesk.invoices import DRAFT_STATUS, InvoiceDraft, PositionDraft
from app.integrations.transport import InnerTransport, PlannedRequest, Reply

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORD_DIR = REPO_ROOT / "tests" / "fixtures" / "sevdesk"
PDF_DIR = Path("~/skillforge-sevdesk-spike")
CUSTOMER_CATEGORY_ID = 3
"""sevDesk's default contact category "customer" - a spike question."""
FOOT_TEXT = "Spike {run_id}: [%KUNDENNUMMER%] [%RECHNUNGSNUMMER%] [%ZAHLUNGSZIEL%]"
"""The foot text under test; the placeholders are candidates, ``discovery`` lists the account's own."""


@dataclass
class Spike:
    client: SevdeskClient
    settings: SevdeskSettings
    run_id: str
    pdf_dir: Path
    foot_text: str
    out: Callable[[str], None]
    contact_id: int | None = None
    invoice_id: int | None = None

    @property
    def tag(self) -> str:
        return f"skillforge-spike:{self.run_id}"


async def version(spike: Spike) -> None:
    spike.out(f"bookkeeping system: {await spike.client.bookkeeping_version()}")


async def custom_fields(spike: Spike) -> None:
    for setting in await spike.client.custom_field_settings():
        spike.out(f"custom field setting {setting.id}: {setting.name}")


async def discovery(spike: Spike) -> None:
    for unity in _objects(await spike.client.http.read("/Unity")):
        spike.out(f"unit {unity.get('id')}: {unity.get('name')}")
    for user in await spike.client.users():
        spike.out(f"user {user.id}")
    for part in await spike.client.parts():
        spike.out(f"part {part.id}: {part.name} (unit {part.unity.id if part.unity else '-'})")
    entries = _objects(
        await spike.client.http.read("/Textparser/fetchDictionaryEntriesByType", params={"objectName": "Invoice"})
    )
    spike.out(f"invoice placeholders: {[entry.get('key') for entry in entries]}")


async def contact(spike: Spike) -> None:
    body = {
        "surename": "Spike",
        "familyname": spike.run_id,
        "category": {"id": CUSTOMER_CATEGORY_ID, "objectName": "Category"},
    }
    spike.contact_id = _created_id(await spike.client.http.write("POST", "/Contact", json=body), "POST /Contact")
    spike.out(f"contact {spike.contact_id} created")


async def invoice(spike: Spike) -> None:
    if spike.contact_id is None or spike.settings.hour_unit_id is None:
        raise SystemExit("invoice needs a contact (--contact-id or the contact step) and SEVDESK__HOUR_UNIT_ID")
    today = date.today()
    draft = InvoiceDraft(
        contact_id=spike.contact_id,
        invoice_date=today,
        delivery_date=today.replace(day=1),
        delivery_date_until=today,
        header=f"Spike {spike.run_id}",
        tax_text=f"Spike {spike.run_id} tax text",
        positions=(
            PositionDraft(
                name=f"Spike {spike.run_id} position",
                minutes=90,
                price_cents=1000,
                unity_id=spike.settings.hour_unit_id,
            ),
        ),
        customer_internal_note=spike.tag,
        foot_text=spike.foot_text,
    )
    saved = _sent(await spike.client.save_invoice(draft))
    spike.invoice_id = saved.invoice.id
    spike.out(f"invoice {saved.invoice.id} saved: status {saved.invoice.status}, {len(saved.positions)} position(s)")


async def read_back(spike: Spike) -> None:
    invoice_id = _invoice_id(spike)
    found = await spike.client.get_invoice(invoice_id)
    if found is None:
        raise SystemExit(f"invoice {invoice_id} not found")
    spike.out(
        f"invoice {found.id}: status {found.status}, number {found.invoice_number}, sum {found.sum_gross_cents} cents"
    )
    spike.out(f"tax rule {found.tax_rule.id if found.tax_rule else '-'}, small settlement {found.small_settlement}")
    spike.out(f"foot text as stored: {found.foot_text!r}")
    for position in await spike.client.invoice_positions(invoice_id):
        spike.out(f"position {position.id}: {position.quantity} x {position.price_cents} cents, unit {position.unity}")
    params = {"customerInternalNote": spike.tag, "limit": 1000}
    listed = _objects(await spike.client.http.read("/Invoice", params=params))
    matching = await spike.client.find_invoices(spike.tag)
    spike.out(f"lookup by customerInternalNote: sevDesk listed {len(listed)}, {len(matching)} carry the tag")


async def pdf(spike: Spike) -> None:
    invoice_id = _invoice_id(spike)
    where = f"POST /Invoice/{invoice_id}/render"
    _readable(
        _sent(await spike.client.http.write("POST", f"/Invoice/{invoice_id}/render", json={"forceReload": True})), where
    )
    params = {"download": True, "preventSendBy": True}
    document = _objects(await spike.client.http.read(f"/Invoice/{invoice_id}/getPdf", params=params))[0]
    content = str(document["content"])
    data = base64.b64decode(content) if document.get("base64encoded") else content.encode()
    path = spike.pdf_dir / f"{spike.run_id}-invoice-{invoice_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    spike.out(f"PDF saved to {path} ({len(data)} bytes)")


async def delete(spike: Spike) -> None:
    invoice_id = _invoice_id(spike)
    found = await spike.client.get_invoice(invoice_id)
    if found is None or found.customer_internal_note != spike.tag or found.status != DRAFT_STATUS:
        raise SystemExit(f"invoice {invoice_id} is no draft of this run ({spike.tag}): not deleted")
    where = f"DELETE /Invoice/{invoice_id}"
    try:
        reply = _readable(_sent(await spike.client.http.write("DELETE", f"/Invoice/{invoice_id}")), where)
    except IntegrationRejectedError as exc:
        spike.out(f"{where} refused: HTTP {exc.status} {exc.remote_message!r}")
        return
    spike.out(f"{where}: HTTP {reply.status}")


async def sequence(spike: Spike) -> None:
    found = await spike.client.invoice_sequence()
    spike.out(f"invoice sequence {found.id}: format {found.format!r}, next {found.next_sequence}")


STEPS: dict[str, Callable[[Spike], Awaitable[None]]] = {
    "version": version,
    "custom-fields": custom_fields,
    "discovery": discovery,
    "contact": contact,
    "invoice": invoice,
    "read-back": read_back,
    "pdf": pdf,
    "delete": delete,
    "sequence": sequence,
}


async def run(
    arguments: argparse.Namespace,
    *,
    settings: SevdeskSettings | None = None,
    transport: InnerTransport | None = None,
    out: Callable[[str], None] = print,
) -> int:
    settings = settings or SevdeskSettings(_env_file=None)
    if settings.mode is not IntegrationMode.LIVE or settings.account is not IntegrationAccount.TRIAL:
        out("the spike runs only with SEVDESK__MODE=live and SEVDESK__ACCOUNT=trial")
        return 1
    pdf_dir = arguments.pdf_dir.expanduser().resolve()
    if pdf_dir.is_relative_to(REPO_ROOT):
        out(f"--pdf-dir must lie outside the repository: {pdf_dir}")
        return 1
    run_id = arguments.run_id or new_run_id(datetime.now(UTC))
    if not RUN_ID.fullmatch(run_id):
        out(f"--run-id is 'run' and 18 digits, as a run prints it: {run_id!r}")
        return 1
    recorder = Recorder(arguments.record_dir, run_id=run_id)
    out(f"run {run_id}")
    try:
        async with SevdeskClient.connect(settings, observer=recorder, transport=transport) as client:
            recorder.arm()
            recorder.step = "identity"
            await client.account()
            spike = Spike(client, settings, run_id, pdf_dir, arguments.foot_text.format(run_id=run_id), out)
            spike.contact_id, spike.invoice_id = arguments.contact_id, arguments.invoice_id
            for name in arguments.step or STEPS:
                _ensure_recorded(recorder)
                recorder.step = name
                out(f"== {name}")
                await STEPS[name](spike)
            _ensure_recorded(recorder)
    except RecordingFailedError as exc:
        out(f"stopped: {exc}")
        return 1
    except OutcomeUnknownError as exc:
        out(f"outcome unknown, not retried: {exc}; look for skillforge-spike:{run_id} in the trial")
        return 1
    except IntegrationError as exc:
        out(f"stopped: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.sevdesk_spike", description=__doc__, allow_abbrev=False)
    parser.add_argument("--step", action="append", choices=list(STEPS), help="Run only this step (repeatable).")
    parser.add_argument("--run-id", help="Continue a run: its id, as the run printed it ('run' and 18 digits).")
    parser.add_argument("--contact-id", type=int, help="The contact for the invoice step, without the contact step.")
    parser.add_argument("--invoice-id", type=int, help="The invoice for read-back, pdf and delete.")
    parser.add_argument("--foot-text", default=FOOT_TEXT, help="Foot text under test; {run_id} is filled in.")
    parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help="Where the recorder writes fixtures.")
    parser.add_argument("--pdf-dir", type=Path, default=PDF_DIR, help="Where the PDF goes; outside the repository.")
    return parser


class RecordingFailedError(Exception):
    """The recorder could not write a fixture: the run stops rather than go on unrecorded."""


def _ensure_recorded(recorder: Recorder) -> None:
    if recorder.failure is not None:
        raise RecordingFailedError(
            f"recording failed ({recorder.failure}) in step {recorder.step}; the fixtures are incomplete"
        )


def _sent[T](written: T | PlannedRequest) -> T:
    if isinstance(written, PlannedRequest):
        raise SystemExit("a write was only planned: the spike needs SEVDESK__MODE=live")
    return written


def _readable(reply: Reply, where: str) -> Reply:
    """A write's answer that can be read; otherwise the write went through with an unknown result."""
    try:
        reply.json()
    except ValueError:
        raise OutcomeUnknownError(f"{where} went through, but its answer is unreadable") from None
    return reply


def _created_id(written: Reply | PlannedRequest, where: str) -> int:
    """The id of the object a write created; an answer without one leaves the outcome unknown."""
    try:
        return int(_objects(_readable(_sent(written), where))[0]["id"])
    except IndexError, KeyError, TypeError, ValueError:
        raise OutcomeUnknownError(f"{where} went through, but its answer names no created object") from None


def _objects(reply: Reply) -> list[dict[str, Any]]:
    """A raw response's objects as a list, for the calls only the spike makes."""
    objects: Any = reply.json()
    if isinstance(objects, dict):
        objects = objects.get("objects", objects)
    return objects if isinstance(objects, list) else [objects]


def _invoice_id(spike: Spike) -> int:
    if spike.invoice_id is None:
        raise SystemExit("this step needs an invoice (--invoice-id or the invoice step)")
    return spike.invoice_id


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))
