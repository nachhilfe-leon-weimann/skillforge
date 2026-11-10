"""Walk the sevDesk spike against the synthetic trial account: `just sevdesk-spike [--step STEP ...]`.

Only the maintainer runs it. It refuses unless ``SEVDESK__MODE=live``, ``SEVDESK__ACCOUNT=trial`` and the identity
check passes; the recorder keeps nothing before that, then records every exchange, starting with the identity read
again and the users, whose names it learns, into ``tests/fixtures/sevdesk/<run id>/``. A write whose exchange cannot
be recorded stops the run before its next call and names the tag to look for; any other failure to record stops it
before the next step. Every object it creates carries the run id - an invoice in ``customerInternalNote`` and its
texts, a contact in its name. A write is never retried: on an unknown outcome, an unreadable answer to a write
included, the step stops and names the run tag to look for in the trial. Its one ``DELETE`` targets a draft that
carries this run's tag. It lives in ``scripts/``, which the image does not contain, so it can never run from a
deployed container.

The steps follow the fixture manifest of ``docs/specs/billing.md`` (S1 to S25). A row the maintainer completes in
the trial's UI - send, edit, cancel, issue, delete a draft - is read afterwards by a step run on its own
(``--run-id``, ``--invoice-id``, ``--step read-back`` or ``--step deleted-read``); the walk itself runs ``STEPS``.
"""

import argparse
import asyncio
import base64
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import (
    IntegrationError,
    IntegrationRejectedError,
    OutcomeUnknownError,
    UnexpectedResponseError,
)
from app.integrations.recording import PLACEHOLDER, RUN_ID, Recorder, new_run_id
from app.integrations.sevdesk.client import ALLOWED_HOSTS, BASE_URL, SevdeskClient, error_message
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.sevdesk.invoices import DRAFT_STATUS, InvoiceDraft, PositionDraft, invoice_payload
from app.integrations.transport import (
    InnerTransport,
    IntegrationHttp,
    Params,
    PlannedRequest,
    Reply,
    network_transport,
    path_template,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORD_DIR = REPO_ROOT / "tests" / "fixtures" / "sevdesk"
PDF_DIR = Path("~/skillforge-sevdesk-spike")
CUSTOMER_CATEGORY_ID = 3
"""sevDesk's default contact category "customer" - a spike question."""
PAY_LINK = "https://pay.example.org/zahlung?re=[%RECHNUNGSNUMMER%]&betrag=[%BETRAG%]"
"""A stand-in for ``{Zahlungslink}``: placeholders for number and amount in a link target."""
PAY_LINK_DOCUMENT_NUMBER = PAY_LINK.replace("[%RECHNUNGSNUMMER%]", "[%DOKUMENTENNUMMER%]")
"""The same link with the number placeholder of today's document title (M9): which of the two resolves is Q2."""
LINE = "<br>\n"
"""How the spec's blocks join their lines (M7): S15 shows what sevDesk makes of the markup and of the line breaks."""
_FOOT_REST = LINE.join((
    "Bitte begleichen Sie den Rechnungsbetrag <strong>bis zum [%ZAHLUNGSZIEL%]</strong> unter Angabe der Rechnungs- "
    "oder Kundennummer.",
    "Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.",
    "Mit freundlichen Grüßen",
    "[%KONTAKTPERSON%]",
))
FOOT_TEXT = (
    f'Spike {{run_id}}: <strong>Zahlungsverfahren:</strong> <em>PayPal (<a href="{PAY_LINK}">Weiter mit PayPal</a>)'
    f"</em>{LINE}{_FOOT_REST}"
)
"""S15: the markup of ``FOOT_PAYPAL`` in billing.md, with candidate placeholders; ``discovery`` lists the account's."""
FOOT_TEXT_DOCUMENT_NUMBER = FOOT_TEXT.replace(PAY_LINK, PAY_LINK_DOCUMENT_NUMBER)
"""S15: ``FOOT_TEXT`` with ``PAY_LINK_DOCUMENT_NUMBER``, on one variant of S11."""
FOOT_TRANSFER = f"Spike {{run_id}}: <strong>Zahlungsverfahren:</strong> <em>Überweisung</em>{LINE}{_FOOT_REST}"
"""S15: ``FOOT_TRANSFER`` of billing.md, on one variant of S11."""
HEAD_TEXT = LINE.join((
    "Spike {run_id}: [%VOLLEANREDE%],",
    "",
    "vielen Dank für Ihren Auftrag und das damit verbundene Vertrauen!",
    "Nachfolgend finden Sie die Abrechnung der vereinbarten Leistungen im oben genannten Zeitraum:",
))
"""S15: ``HEAD_TEXT`` of billing.md, with today's placeholder for the full salutation (M3)."""
DOCUMENT_TITLE = "Spike {run_id}: Rechnung Nr. [%DOKUMENTENNUMMER%]"
"""S14: ``DOCUMENT_TITLE`` of billing.md as the ``header`` of one variant of S11 (M9)."""
TOPUP_TEXT = "Vorauszahlung für kommende Unterrichtsstunden"
"""S11: ``TOPUP_POSITION_DESCRIPTION`` of billing.md (M4)."""
LESSON_TEXT = "Montag, 3. November 2025 / 14:00 – 15:30"
"""A lesson's description in the form of ``POSITION_DESCRIPTION`` (billing.md); synthetic."""
PAYMENT_METHODS = ("PayPal", "Überweisung")
LISTING_LIMIT = 1000
SAVE = "POST /Invoice/Factory/saveInvoice"
WRONG_TOKEN = "skillforge-spike-wrong-token"


class CutConnection(httpx.AsyncBaseTransport):
    """Hands every request on; once armed, it drops the answer of the next write after it was sent (S20)."""

    def __init__(self, inner: InnerTransport) -> None:
        self._inner = inner
        self.armed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if self.armed and request.method != "GET":
            self.armed = False
            await response.aclose()
            raise httpx.ReadError("the spike cut the connection after the request went out", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Spike:
    client: SevdeskClient
    settings: SevdeskSettings
    run_id: str
    pdf_dir: Path
    foot_text: str
    out: Callable[[str], None]
    head_text: str | None = None
    cut: CutConnection | None = None
    recorder: Recorder | None = None
    mock: InnerTransport | None = None
    """The test's transport, if any; a client of its own (the wrong token) otherwise gets a fresh network one."""
    contact_id: int | None = None
    invoice_id: int | None = None
    part_id: int | None = None

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
    for part in await _listing(spike, "/Part"):
        spike.out(f"part {part.get('id')}: {part.get('name')} (unit {_ref_id(part.get('unity'))})")
        if spike.part_id is None:
            spike.part_id = int(part["id"])
            spike.out(f"S11 uses part {spike.part_id}")
    groups = _objects(
        await spike.client.http.read("/Textparser/fetchDictionaryEntriesByType", params={"objectName": "Invoice"}),
        "GET /Textparser/fetchDictionaryEntriesByType",
    )
    for group in groups:
        for entry in group.get("value") or []:
            value = entry.get("value")
            shown = value if isinstance(value, str) and PLACEHOLDER.fullmatch(value) else "<not a placeholder>"
            spike.out(f"placeholder {group.get('key')}.{entry.get('key')}: {shown}")


async def contact(spike: Spike) -> None:
    spike.contact_id = await _new_contact(spike, "Spike")


async def contact_reads(spike: Spike) -> None:
    found = await spike.client.get_contact(_contact_id(spike))
    if found is None:
        raise SystemExit(f"contact {spike.contact_id} not found")
    spike.out(f"contact {found.id}: customer number {found.customer_number}")
    if found.customer_number is not None:
        params = {"customerNumber": found.customer_number, "depth": 1}
        listed = _objects(await spike.client.http.read("/Contact", params=params))
        spike.out(f"by customer number: {[item.get('id') for item in listed]}")


async def payment_methods(spike: Spike) -> None:
    setting_id = spike.settings.payment_method_field_setting_id
    setting = None if setting_id is None else await spike.client.get_custom_field_setting(setting_id)
    if setting is None:
        raise SystemExit("payment-methods needs SEVDESK__PAYMENT_METHOD_FIELD_SETTING_ID of an existing setting")
    contacts = {"none": _contact_id(spike)}
    for method in PAYMENT_METHODS:
        contacts[method] = await _new_contact(spike, f"Spike {method}")
        field = {
            "objectName": "ContactCustomField",
            "contact": {"id": contacts[method], "objectName": "Contact"},
            "contactCustomFieldSetting": {"id": setting.id, "objectName": "ContactCustomFieldSetting"},
            "value": method,
        }
        written = await _write(
            spike, "POST /ContactCustomField", spike.client.http.write("POST", "/ContactCustomField", json=field)
        )
        _created_id(written, "POST /ContactCustomField")
    for method, contact_id in contacts.items():
        params = {"contact[id]": contact_id, "contact[objectName]": "Contact"}
        fields = await _listing(spike, "/ContactCustomField", params)
        values = [field.get("value") for field in fields if _ref_id(field.get("contact")) == contact_id]
        spike.out(f"contact {contact_id} ({method}): payment method values {values}")
    for method in PAYMENT_METHODS:
        found = await spike.client.find_contacts_by_custom_field(setting.name, method)
        spike.out(f"contacts paying by {method}: {[item.id for item in found]}")


async def invoice(spike: Spike) -> None:
    spike.invoice_id = await _save(spike, _draft(spike, _lesson(spike, "position", cents=1000)), "invoice")


async def invoice_variants(spike: Spike) -> None:
    lessons = (_lesson(spike, "lesson 1"), _lesson(spike, "lesson 2", 60), _lesson(spike, "lesson 3", 45))
    children = (
        _lesson(spike, "student B", text=f"Spike B / {LESSON_TEXT}"),
        _lesson(spike, "student C", 60, text=f"Spike C / {LESSON_TEXT}"),
    )
    variants = {
        "several lessons": _draft(spike, *lessons),
        "several children": _draft(spike, *children),
        "the transfer foot text": _draft(spike, _lesson(spike, "transfer"), foot_text=_text(spike, FOOT_TRANSFER)),
        "the document title": _draft(spike, _lesson(spike, "title"), header=_text(spike, DOCUMENT_TITLE)),
        "the pay link with the document number": _draft(
            spike, _lesson(spike, "pay link"), foot_text=_text(spike, FOOT_TEXT_DOCUMENT_NUMBER)
        ),
    }
    for label, draft in variants.items():
        await _save(spike, draft, label)
    if spike.part_id is None:
        spike.out("a top-up and a part without a name: no part (--part-id, or a part the discovery step finds)")
        return
    top_up = _lesson(spike, "5 x Maths", 300, part_id=spike.part_id, text=TOPUP_TEXT)
    await _save(spike, _draft(spike, top_up), "a top-up")
    # Q5: a position with a part and no name - built as always, then sent without its name; a refusal is the answer.
    draft = _draft(spike, _lesson(spike, "unnamed", part_id=spike.part_id, text=None))
    body = invoice_payload(draft, contact_person_id=_contact_person(spike))
    for position in cast(list[dict[str, object]], body["invoicePosSave"]):
        del position["name"]
    try:
        written = await _write(spike, SAVE, spike.client.http.write("POST", "/Invoice/Factory/saveInvoice", json=body))
    except IntegrationRejectedError as exc:
        spike.out(f"a part and no name: refused, HTTP {exc.status}, {exc.remote_message!r}")
        return
    invoice_id = _created_id(written, SAVE, "invoice")
    names = [position.get("name") for position in await _listing(spike, f"/Invoice/{invoice_id}/getPositions")]
    spike.out(f"a part and no name: invoice {invoice_id}, its positions named {names}")


async def rounding(spike: Spike) -> None:
    positions = tuple(
        _lesson(spike, f"{minutes} min at {cents}", minutes, cents)
        for minutes in (15, 45, 90)
        for cents in (2996, 3000, 4500)
    )
    invoice_id = await _save(spike, _draft(spike, *positions), "rounding")
    local = sum(position.minutes * position.price_cents // 60 for position in positions)
    found = await spike.client.get_invoice(invoice_id)
    remote = (found.sum_net_cents, found.sum_gross_cents) if found else None
    verdict = "equal" if remote == (local, local) else "DIFFERENT"
    spike.out(f"rounding: local {local} cents, sevDesk net and gross {remote}: {verdict}")
    for position in await _listing(spike, f"/Invoice/{invoice_id}/getPositions"):
        spike.out(f"position {position.get('id')}: {position.get('quantity')} x {position.get('price')}")


async def numbers(spike: Spike) -> None:
    found = await spike.client.invoice_sequence()
    for offset, label in enumerate(("first", "second")):
        number = found.format.replace("[%NUMBER%]", str(found.next_sequence + offset))
        body = invoice_payload(
            _draft(spike, _lesson(spike, f"{label} numbered")), contact_person_id=_contact_person(spike)
        )
        body["invoice"] = {**_dict(body["invoice"]), "invoiceNumber": number}
        written = await _write(spike, SAVE, spike.client.http.write("POST", "/Invoice/Factory/saveInvoice", json=body))
        invoice_id = _created_id(written, SAVE, "invoice")
        stored = await spike.client.get_invoice(invoice_id)
        spike.out(
            f"{label} draft {invoice_id}: number sent {number}, stored {stored.invoice_number if stored else None}"
        )
    spike.out("issue both in the trial in either order, then: --step read-back --invoice-id <each id>")


async def delivery_dates(spike: Spike) -> None:
    for label, (start, until) in s24_dates(date.today()).items():
        draft = _draft(spike, _lesson(spike, label), delivery_date=start, delivery_date_until=until)
        invoice_id = await _save(spike, draft, label)
        stored = _objects(await spike.client.http.read(f"/Invoice/{invoice_id}"))[0]
        stored_dates = f"{stored.get('deliveryDate')} to {stored.get('deliveryDateUntil')}"
        spike.out(f"{label}: sent {start} to {until}, stored {stored_dates}")


async def cut_connection(spike: Spike) -> None:
    if spike.cut is None:
        raise SystemExit("cut-connection needs the spike's own transport")
    reference = f"{spike.tag}:S20"
    spike.cut.armed = True
    try:
        await _write(
            spike,
            SAVE,
            spike.client.save_invoice(_draft(spike, _lesson(spike, "cut"), customer_internal_note=reference)),
        )
        raise SystemExit("the connection was not cut")
    except OutcomeUnknownError as exc:
        spike.out(f"pending, then unknown: {exc}")
    finally:
        spike.cut.armed = False
    listed = await _listing(spike, "/Invoice", {"customerInternalNote": reference})
    spike.out(f"the filter on customerInternalNote alone listed {[item.get('id') for item in listed]}")
    params = {"contact[id]": _contact_id(spike), "contact[objectName]": "Contact", "status": DRAFT_STATUS}
    drafts = await _listing(spike, "/Invoice", params)
    spike.out(f"the contact's drafts: {[item.get('id') for item in drafts]}")
    reconciled = [item.get("id") for item in listed if item.get("customerInternalNote") == reference]
    spike.out(f"reconciled by reference: {reconciled}")


async def error_bodies(spike: Spike) -> None:
    invalid = {"invoice": {"objectName": "Invoice", "mapAll": True, "customerInternalNote": f"{spike.tag}:S21"}}
    try:
        reply = await _write(spike, SAVE, spike.client.http.write("POST", "/Invoice/Factory/saveInvoice", json=invalid))
        spike.out(f"a draft without contact and positions was accepted: HTTP {reply.status}")
    except (IntegrationRejectedError, OutcomeUnknownError) as exc:
        if exc.status is None:
            raise
        spike.out(f"a draft without contact and positions: HTTP {exc.status}, {exc.remote_message!r}")
    wrong = IntegrationHttp(
        name="sevdesk",
        base_url=BASE_URL,
        allowed_hosts=ALLOWED_HOSTS,
        headers={"Accept": "application/json"},
        credentials={"Authorization": WRONG_TOKEN},
        mode=spike.settings.mode,
        account=spike.settings.account,
        error_message=error_message,
        observer=spike.recorder,
        transport=spike.mock,
    )
    async with wrong as http:
        try:
            await http.read("/SevClient")
            spike.out("a wrong token was accepted")
        except IntegrationRejectedError as exc:
            spike.out(f"a wrong token: HTTP {exc.status}, {exc.remote_message!r}")


async def deleted_read(spike: Spike) -> None:
    """S25, after the maintainer deleted the draft in the trial's UI."""
    invoice_id = _invoice_id(spike)
    try:
        found = _objects(await spike.client.http.read(f"/Invoice/{invoice_id}"))
        spike.out(f"GET /Invoice/{invoice_id} after the deletion: {len(found)} object(s)")
    except IntegrationRejectedError as exc:
        spike.out(f"GET /Invoice/{invoice_id} after the deletion: HTTP {exc.status}")
    params = {"contact[id]": _contact_id(spike), "contact[objectName]": "Contact"}
    listed = [item.get("id") for item in await _listing(spike, "/Invoice", params)]
    spike.out(
        f"the contact's invoices: {listed}; {invoice_id} {'still listed' if str(invoice_id) in listed else 'gone'}"
    )


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
    for position in await _listing(spike, f"/Invoice/{invoice_id}/getPositions"):
        unit = _ref_id(position.get("unity"))
        spike.out(f"position {position.get('id')}: {position.get('quantity')} x {position.get('price')}, unit {unit}")
    listed = await _listing(spike, "/Invoice", {"customerInternalNote": spike.tag})
    matching = [item for item in listed if item.get("customerInternalNote") == spike.tag]
    spike.out(f"lookup by customerInternalNote: sevDesk listed {len(listed)}, {len(matching)} carry the tag")


async def pdf(spike: Spike) -> None:
    invoice_id = _invoice_id(spike)
    where = f"POST /Invoice/{invoice_id}/render"
    _readable(
        await _write(
            spike, where, spike.client.http.write("POST", f"/Invoice/{invoice_id}/render", json={"forceReload": True})
        ),
        where,
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
        reply = _readable(
            await _write(spike, where, spike.client.http.write("DELETE", f"/Invoice/{invoice_id}")), where
        )
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
    "contact-reads": contact_reads,
    "payment-methods": payment_methods,
    "invoice": invoice,
    "read-back": read_back,
    "pdf": pdf,
    "invoice-variants": invoice_variants,
    "rounding": rounding,
    "numbers": numbers,
    "delivery-dates": delivery_dates,
    "cut-connection": cut_connection,
    "error-bodies": error_bodies,
    "delete": delete,
    "sequence": sequence,
}
FOLLOW_UPS: dict[str, Callable[[Spike], Awaitable[None]]] = {"deleted-read": deleted_read}
"""Steps that read what the maintainer did in the trial's UI: never part of the walk, run with ``--step``."""


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
    cut = CutConnection(transport or network_transport())
    out(f"run {run_id}")
    try:
        async with SevdeskClient.connect(settings, observer=recorder, transport=cut) as client:
            recorder.arm()
            recorder.step = "identity"
            await client.account()
            await client.users()  # its names are learned before any step, a step run on its own included
            spike = Spike(
                client,
                settings,
                run_id,
                pdf_dir,
                arguments.foot_text.replace("{run_id}", run_id),
                out,
                head_text=arguments.head_text.replace("{run_id}", run_id),
                cut=cut,
                recorder=recorder,
                mock=transport,
                contact_id=arguments.contact_id,
                invoice_id=arguments.invoice_id,
                part_id=arguments.part_id,
            )
            for name in arguments.step or STEPS:
                _ensure_recorded(recorder)
                recorder.step = name
                out(f"== {name}")
                await (STEPS | FOLLOW_UPS)[name](spike)
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
    parser.add_argument(
        "--step", action="append", choices=[*STEPS, *FOLLOW_UPS], help="Run only this step (repeatable)."
    )
    parser.add_argument("--run-id", help="Continue a run: its id, as the run printed it ('run' and 18 digits).")
    parser.add_argument("--contact-id", type=int, help="The contact for the invoice step, without the contact step.")
    parser.add_argument("--invoice-id", type=int, help="The invoice for read-back, pdf and delete.")
    parser.add_argument(
        "--part-id", type=int, help="The part of the S11 top-up; the discovery step takes the first part otherwise."
    )
    parser.add_argument("--foot-text", default=FOOT_TEXT, help="Foot text under test; {run_id} is filled in.")
    parser.add_argument("--head-text", default=HEAD_TEXT, help="Head text under test; {run_id} is filled in.")
    parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help="Where the recorder writes fixtures.")
    parser.add_argument("--pdf-dir", type=Path, default=PDF_DIR, help="Where the PDF goes; outside the repository.")
    return parser


class RecordingFailedError(Exception):
    """The recorder could not write a fixture: the run stops rather than go on unrecorded."""


def _ensure_recorded(recorder: Recorder | None, consequence: str = "the fixtures are incomplete") -> None:
    if recorder is not None and recorder.failure is not None:
        raise RecordingFailedError(f"recording failed ({recorder.failure}) in step {recorder.step}; {consequence}")


def _sent[T](written: T | PlannedRequest) -> T:
    if isinstance(written, PlannedRequest):
        raise SystemExit("a write was only planned: the spike needs SEVDESK__MODE=live")
    return written


async def _write[T](spike: Spike, where: str, call: Coroutine[Any, Any, T | PlannedRequest]) -> T:
    """Send one write (``where``: method and path); whatever its outcome, the run stops right here if its exchange
    could not be recorded, and names what went out and the tag to look for."""
    method, _, path = where.partition(" ")
    try:
        _ensure_recorded(spike.recorder)  # nothing goes out after an exchange that could not be recorded
    except RecordingFailedError:
        call.close()
        raise
    try:
        written = await call
    finally:
        _ensure_recorded(spike.recorder, f"{method} {path_template(path)} went out; look for {spike.tag} in the trial")
    return _sent(written)


async def _listing(spike: Spike, path: str, params: Params | None = None) -> list[dict[str, Any]]:
    """A listing read raw with ``countAll``: whether ``total`` came back is Q18's answer, and the walk goes on."""
    reply = await spike.client.http.read(path, params={**(params or {}), "countAll": True, "limit": LISTING_LIMIT})
    body = _json(reply, f"GET {path_template(path)}")
    total = body.get("total") if isinstance(body, dict) else None
    found = _as_list(body)
    spike.out(f"Q18 {path_template(path)}: total {total!r}, {len(found)} object(s)")
    return found


def _readable(reply: Reply, where: str) -> Reply:
    """A write's answer that can be read; otherwise the write went through with an unknown result."""
    try:
        reply.json()
    except ValueError, RecursionError:
        raise OutcomeUnknownError(f"{where} went through, but its answer is unreadable") from None
    return reply


def _created_id(written: Reply, where: str, *inside: str) -> int:
    """The id of the object a write created, below the keys ``inside``; an answer without one leaves the outcome
    unknown."""
    try:
        created = _objects(_readable(written, where))[0]
        for key in inside:
            created = created[key]
        return int(created["id"])
    except IndexError, KeyError, TypeError, ValueError:
        raise OutcomeUnknownError(f"{where} went through, but its answer names no created object") from None


async def _new_contact(spike: Spike, first_name: str) -> int:
    body = {
        "surename": first_name,
        "familyname": spike.run_id,
        "category": {"id": CUSTOMER_CATEGORY_ID, "objectName": "Category"},
    }
    contact_id = _created_id(
        await _write(spike, "POST /Contact", spike.client.http.write("POST", "/Contact", json=body)), "POST /Contact"
    )
    spike.out(f"contact {contact_id} created")
    return contact_id


def _lesson(
    spike: Spike,
    name: str,
    minutes: int = 90,
    cents: int = 3000,
    *,
    part_id: int | None = None,
    text: str | None = LESSON_TEXT,
) -> PositionDraft:
    if spike.settings.hour_unit_id is None:
        raise SystemExit("a draft needs SEVDESK__HOUR_UNIT_ID")
    return PositionDraft(
        name=f"Spike {spike.run_id} {name}",
        minutes=minutes,
        price_cents=cents,
        unity_id=spike.settings.hour_unit_id,
        part_id=part_id,
        text=text,
    )


def _text(spike: Spike, template: str) -> str:
    return template.replace("{run_id}", spike.run_id)


def _draft(spike: Spike, *positions: PositionDraft, **changes: Any) -> InvoiceDraft:
    """A draft of this run: tagged, with the texts under test; ``changes`` replace its fields."""
    today = date.today()
    draft = InvoiceDraft(
        contact_id=_contact_id(spike),
        invoice_date=today,
        delivery_date=today.replace(day=1),
        delivery_date_until=today,
        header=f"Spike {spike.run_id}",
        tax_text=f"Spike {spike.run_id} tax text",
        positions=positions,
        customer_internal_note=spike.tag,
        head_text=spike.head_text,
        foot_text=spike.foot_text,
    )
    return replace(draft, **changes)


async def _save(spike: Spike, draft: InvoiceDraft, label: str) -> int:
    saved = await _write(spike, SAVE, spike.client.save_invoice(draft))
    spike.out(
        f"{label}: invoice {saved.invoice.id} saved, status {saved.invoice.status}, {len(saved.positions)} position(s)"
    )
    return saved.invoice.id


def _contact_id(spike: Spike) -> int:
    if spike.contact_id is None:
        raise SystemExit("this step needs a contact (--contact-id or the contact step)")
    return spike.contact_id


def _contact_person(spike: Spike) -> int:
    if spike.settings.contact_person_id is None:
        raise SystemExit("this step needs SEVDESK__CONTACT_PERSON_ID")
    return spike.settings.contact_person_id


def _ref_id(value: object) -> int | None:
    """The id of a reference (``{"id": "12", "objectName": "Unity"}``) in a raw answer."""
    return int(value["id"]) if isinstance(value, dict) and value.get("id") is not None else None


def s24_dates(today: date) -> dict[str, tuple[date, date | None]]:
    """S24's delivery dates, all in the year before ``today``: sevDesk refuses a delivery date after the invoice date
    (decision L), and a draft's invoice date is the day it is created."""
    year = today.year - 1
    spring, autumn = _last_sunday(year, 3), _last_sunday(year, 10)
    return {
        "the spring clock change": (spring, None),
        "a period over the autumn clock change": (autumn - timedelta(days=1), autumn),
        "the last day of the year": (date(year, 12, 31), None),
    }


def _last_sunday(year: int, month: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def _json(reply: Reply, where: str) -> Any:
    """A raw answer's JSON; one that is none, or nested too deep to parse, stops the run with a message."""
    try:
        return reply.json()
    except ValueError, RecursionError:
        raise UnexpectedResponseError(f"sevdesk {where}: the answer is no readable JSON") from None


def _objects(reply: Reply, where: str = "read") -> list[dict[str, Any]]:
    """A raw response's objects as a list, for the calls only the spike makes."""
    return _as_list(_json(reply, where))


def _as_list(objects: Any) -> list[dict[str, Any]]:
    if isinstance(objects, dict):
        objects = objects.get("objects", objects)
    return objects if isinstance(objects, list) else [objects]


def _invoice_id(spike: Spike) -> int:
    if spike.invoice_id is None:
        raise SystemExit("this step needs an invoice (--invoice-id or the invoice step)")
    return spike.invoice_id


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))
