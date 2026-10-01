"""The operator commands parse their arguments before they touch the database."""

from pathlib import Path
from uuid import UUID

import pytest

from app.cli import bootstrap
from app.cli.bootstrap import build_parser

JUSTFILE = Path(__file__).resolve().parents[2] / "justfile"


def test_there_is_no_skillbot_command(capsys):
    """skillbot's client is set up with the client command like any other (bot-decoupling spec, P0-5)."""
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["skillbot"])

    assert exit_code.value.code == 2
    assert "invalid choice: 'skillbot'" in capsys.readouterr().err


def test_the_client_command_takes_a_client_id_and_a_scope_list_per_mode():
    arguments = build_parser().parse_args([
        "client",
        "operator",
        "--application",
        "auth:users:login crm:write",
        "--delegated",
        " account:self  crm:read ",
    ])

    assert arguments.command == "client"
    assert arguments.client_id == "operator"
    assert arguments.application == frozenset({"auth:users:login", "crm:write"})
    assert arguments.delegated == frozenset({"account:self", "crm:read"})


def test_an_empty_scope_list_grants_nothing_in_that_mode():
    arguments = build_parser().parse_args(["client", "operator", "--application", "", "--delegated", "crm:read"])

    assert arguments.application == frozenset()


@pytest.mark.parametrize(
    ("given", "missing"),
    [(["--delegated", "crm:read"], "--application"), (["--application", "crm:write"], "--delegated")],
)
def test_the_client_command_needs_both_scope_lists(given: list[str], missing: str, capsys):
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["client", "operator", *given])

    assert exit_code.value.code == 2
    assert missing in capsys.readouterr().err


def test_the_client_id_is_stripped():
    arguments = build_parser().parse_args(["client", " operator ", "--application", "", "--delegated", ""])

    assert arguments.client_id == "operator"


@pytest.mark.parametrize("client_id", ["", "   "])
def test_an_empty_client_id_is_refused(client_id: str, capsys):
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["client", client_id, "--application", "", "--delegated", ""])

    assert exit_code.value.code == 2
    assert "client_id: must not be empty" in capsys.readouterr().err


def test_main_runs_the_client_command_with_the_scope_list_of_each_mode(monkeypatch):
    calls = []

    async def bootstrap_client(client_id: str, **scopes: frozenset[str]) -> None:
        calls.append(("client", client_id, scopes))

    monkeypatch.setattr(bootstrap, "bootstrap_client", bootstrap_client)
    monkeypatch.setattr(
        "sys.argv", ["bootstrap", "client", "op", "--application", "crm:write", "--delegated", "crm:read"]
    )

    bootstrap.main()

    assert calls == [("client", "op", {"application": frozenset({"crm:write"}), "delegated": frozenset({"crm:read"})})]


def test_the_just_recipe_runs_the_client_subcommand():
    """`bootstrap-client` hands its arguments on as they were quoted: `{{ args }}` would split a scope list."""
    assert (
        '[positional-arguments]\nbootstrap-client *args:\n    uv run python -m app.cli.bootstrap client "$@"\n'
        in JUSTFILE.read_text()
    )


PARTY_ID = "7d9f4f3e-1c2b-4a5d-9e8f-0a1b2c3d4e5f"


def test_the_admin_command_takes_a_party_id_and_an_email():
    arguments = build_parser().parse_args(["admin", "--party-id", PARTY_ID, "--email", "admin@example.org"])

    assert arguments.command == "admin"
    assert arguments.party_id == UUID(PARTY_ID)
    assert arguments.email == "admin@example.org"


@pytest.mark.parametrize(
    ("given", "missing"),
    [(["--email", "admin@example.org"], "--party-id"), (["--party-id", PARTY_ID], "--email")],
)
def test_the_admin_command_needs_both_options(given: list[str], missing: str, capsys):
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["admin", *given])

    assert exit_code.value.code == 2
    assert missing in capsys.readouterr().err


def test_the_admin_command_refuses_a_party_id_that_is_no_uuid(capsys):
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["admin", "--party-id", "anna", "--email", "admin@example.org"])

    assert exit_code.value.code == 2
    assert "--party-id" in capsys.readouterr().err


@pytest.mark.parametrize("email", ["", "admin", "admin@", "@example.org", "admin@example", f"{'a' * 250}@example.org"])
def test_the_admin_command_validates_the_email_by_the_apis_rule(email: str, capsys):
    """The rule of `LoginEmail`: an invalid address is an argparse error before anything is written."""
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["admin", "--party-id", PARTY_ID, "--email", email])

    assert exit_code.value.code == 2
    assert "--email: not a valid e-mail address" in capsys.readouterr().err


def test_main_runs_the_admin_command(monkeypatch):
    calls = []

    async def bootstrap_admin(**arguments: object) -> None:
        calls.append(("admin", arguments))

    monkeypatch.setattr(bootstrap, "bootstrap_admin", bootstrap_admin)
    monkeypatch.setattr("sys.argv", ["bootstrap", "admin", "--party-id", PARTY_ID, "--email", "admin@example.org"])

    bootstrap.main()

    assert calls == [("admin", {"party_id": UUID(PARTY_ID), "email": "admin@example.org"})]


def test_the_just_recipe_runs_the_admin_subcommand():
    assert (
        '[positional-arguments]\nbootstrap-admin *args:\n    uv run python -m app.cli.bootstrap admin "$@"\n'
        in JUSTFILE.read_text()
    )
