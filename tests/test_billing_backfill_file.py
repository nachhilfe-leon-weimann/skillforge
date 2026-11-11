"""`just billing-backfill`: the path guard and the file format, both settled before anything connects (billing spec,
SF3-2).

The file lies outside this repository and every other git work tree, symlinks resolved; a relative path, a directory
and a missing file are refused too. The refusal comes before the file is read and before the settings are read or a
connection is opened. Whitespace in a path is fine: the recipe hands its arguments on as quoted.
"""

import os
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import billing_backfill
from app.cli.billing_backfill import COLUMNS, REPO_ROOT, MappingRefusedError, Problem, Reason, check_path, read_mapping

STUDENT = "5b3c2a10-0000-4000-8000-00000000000b"
PAYER = "5b3c2a10-0000-4000-8000-00000000000a"
HEADER = ",".join(COLUMNS)
WORK_TREE = "the file lies inside a git work tree"


def _mapping(folder: Path, *rows: str, header: str = HEADER, name: str = "mapping.csv") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _row(**cells: str) -> str:
    return ",".join(cells.get(column, "") for column in COLUMNS)


def _work_tree(tmp_path: Path, marker: str = "directory") -> Path:
    tree = tmp_path / "other-checkout"
    tree.mkdir()
    if marker == "directory":
        (tree / ".git").mkdir()
    else:
        (tree / ".git").write_text("gitdir: synthetic\n")  # a linked worktree's .git is a file
    return tree


# --- The path ---------------------------------------------------------------------------------------------------


def test_a_relative_path_is_refused(tmp_path: Path, monkeypatch):
    """`just` runs its recipes in the repository: a relative path would mean another folder than the shell's."""
    _mapping(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(MappingRefusedError, match="absolute path"):
        check_path("mapping.csv")


def test_a_file_inside_this_repository_is_refused():
    with pytest.raises(MappingRefusedError, match=WORK_TREE):
        check_path(str(REPO_ROOT / "pyproject.toml"))


def test_a_path_through_a_symlink_into_this_repository_is_refused(tmp_path: Path):
    (tmp_path / "linked.csv").symlink_to(REPO_ROOT / "pyproject.toml")
    (tmp_path / "linked-folder").symlink_to(REPO_ROOT)

    with pytest.raises(MappingRefusedError, match=WORK_TREE):
        check_path(str(tmp_path / "linked.csv"))
    with pytest.raises(MappingRefusedError, match=WORK_TREE):
        check_path(str(tmp_path / "linked-folder" / "pyproject.toml"))


@pytest.mark.parametrize("marker", ["directory", "file"])
def test_a_file_inside_any_git_work_tree_is_refused(tmp_path: Path, marker: str):
    path = _mapping(_work_tree(tmp_path, marker) / "deep" / "folder")

    with pytest.raises(MappingRefusedError, match=WORK_TREE):
        check_path(str(path))


def test_a_symlink_that_lies_in_a_work_tree_is_refused_wherever_it_points(tmp_path: Path):
    """The link itself could be committed - and a `..` after it leads where the link points."""
    target = _mapping(tmp_path / "outside")
    link = _work_tree(tmp_path) / "mapping.csv"
    link.symlink_to(target)

    with pytest.raises(MappingRefusedError, match=WORK_TREE):
        check_path(str(link))


def test_a_directory_and_a_missing_file_are_refused(tmp_path: Path):
    with pytest.raises(MappingRefusedError, match="no regular file"):
        check_path(str(tmp_path))
    with pytest.raises(MappingRefusedError, match="does not exist"):
        check_path(str(tmp_path / "missing.csv"))
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    with pytest.raises(MappingRefusedError, match="does not exist"):
        check_path(str(tmp_path / "loop"))


def test_whitespace_in_the_path_is_accepted(tmp_path: Path):
    path = _mapping(tmp_path / "confirmed mappings", _row(student_party_id=STUDENT), name="family a.csv")

    assert check_path(str(path)) == path.resolve()
    assert read_mapping(str(path)).count == 1


def test_a_parent_step_after_a_symlink_leads_where_the_link_points(tmp_path: Path):
    """`link/../mapping.csv` is the file the OS opens: the one next to the link's target, not next to the link."""
    decoy = _mapping(tmp_path, _row(student_party_id=PAYER))
    meant = _mapping(tmp_path / "real", _row(student_party_id=STUDENT))
    (tmp_path / "real" / "sub").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real" / "sub")

    path = str(tmp_path / "link" / ".." / "mapping.csv")

    assert check_path(path) == meant.resolve() != decoy.resolve()
    assert [str(row.student_id) for row in read_mapping(path).rows] == [STUDENT]


def test_a_symlink_outside_every_work_tree_is_followed(tmp_path: Path):
    target = _mapping(tmp_path / "real", _row(student_party_id=STUDENT))
    (tmp_path / "link.csv").symlink_to(target)

    assert check_path(str(tmp_path / "link.csv")) == target.resolve()


@pytest.mark.parametrize("where", ["work tree", "relative", "missing", "directory"])
async def test_a_bad_path_is_refused_before_the_file_is_read_and_before_anything_connects(
    tmp_path: Path, monkeypatch, where: str
):
    """The file in the work tree is unreadable: reading it first would fail with another error than the refusal."""
    unreadable = _mapping(_work_tree(tmp_path), _row(student_party_id=STUDENT))
    unreadable.chmod(0)
    paths = {
        "work tree": str(unreadable),
        "relative": os.path.relpath(unreadable),
        "missing": str(tmp_path / "missing.csv"),
        "directory": str(tmp_path),
    }
    monkeypatch.setattr(billing_backfill, "get_settings", lambda: pytest.fail("no settings expected"))
    monkeypatch.setattr(billing_backfill, "Database", SimpleNamespace(from_url=lambda url: pytest.fail("no database")))

    try:
        with pytest.raises(SystemExit, match="^billing-backfill refused: ") as refused:
            await billing_backfill.billing_backfill(paths[where], apply=True)
    finally:
        unreadable.chmod(0o600)

    assert str(tmp_path) not in str(refused.value.code)


async def test_an_unreadable_file_is_refused_without_its_path_and_before_anything_connects(tmp_path: Path, monkeypatch):
    """On macOS a folder the terminal may not read (`~/Documents`) fails the same way as a file without rights."""
    path = _mapping(tmp_path / "family synthetic", _row(student_party_id=STUDENT))
    path.chmod(0)
    monkeypatch.setattr(billing_backfill, "Database", SimpleNamespace(from_url=lambda url: pytest.fail("no database")))

    try:
        with pytest.raises(SystemExit) as refused:
            await billing_backfill.billing_backfill(str(path), apply=True)
    finally:
        path.chmod(0o600)

    assert refused.value.code == "billing-backfill refused: the file cannot be read"


# --- The connection ---------------------------------------------------------------------------------------------

SECRET_URL = "postgresql+asyncpg://maintainer:secret-pw-4711@127.0.0.1:1/skillforge"


def _broken_settings() -> SimpleNamespace:
    """What pydantic does with a bad setting: its message repeats the value."""
    raise ValueError(f"invalid DB__URL {SECRET_URL}")


@pytest.mark.parametrize(
    "settings",
    [
        _broken_settings,
        lambda: SimpleNamespace(db=SimpleNamespace(url="no url secret-pw-4711@127.0.0.1")),
    ],
    ids=["settings error", "unparsable URL"],
)
async def test_a_settings_or_url_error_prints_neither_the_url_nor_its_message(tmp_path: Path, monkeypatch, settings):
    path = _mapping(tmp_path, _row(student_party_id=STUDENT))
    monkeypatch.setattr(billing_backfill, "get_settings", settings)

    with pytest.raises(SystemExit) as refused:
        await billing_backfill.billing_backfill(str(path), apply=False)

    assert refused.value.code == "billing-backfill refused: the database settings cannot be read"


async def test_a_refused_connection_prints_its_class_only(tmp_path: Path, monkeypatch, capsys):
    """A refused connection is an `OSError` SQLAlchemy does not wrap; its message names the host and the port."""
    path = _mapping(tmp_path, _row(student_party_id=STUDENT))
    monkeypatch.setattr(billing_backfill, "get_settings", lambda: SimpleNamespace(db=SimpleNamespace(url=SECRET_URL)))

    with pytest.raises(SystemExit) as failed:
        await billing_backfill.billing_backfill(str(path), apply=True)

    output = str(failed.value.code) + "".join(capsys.readouterr())
    assert re.fullmatch(r"billing-backfill failed, nothing written: [A-Za-z]+Error", str(failed.value.code))
    assert [secret for secret in ("127.0.0.1", "secret-pw-4711", "Connect call") if secret in output] == []


# --- The file ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        ",".join(COLUMNS[:-1]),  # a column missing
        f"{HEADER},note",  # an unknown column
        f"{HEADER},rate_valid_from",  # a column twice
        HEADER.upper(),
        "",
    ],
)
async def test_a_header_that_is_not_exactly_the_columns_refuses_the_file_before_anything_connects(
    tmp_path: Path, monkeypatch, header: str
):
    path = _mapping(tmp_path, _row(student_party_id=STUDENT), header=header)
    monkeypatch.setattr(billing_backfill, "Database", SimpleNamespace(from_url=lambda url: pytest.fail("no database")))

    with pytest.raises(SystemExit, match="^billing-backfill refused: the header must name each of these columns"):
        await billing_backfill.billing_backfill(str(path), apply=False)


def test_the_columns_may_come_in_any_order(tmp_path: Path):
    cells = {"student_party_id": STUDENT, "rate_cents_per_hour": "3200", "rate_valid_from": "2026-01-01"}
    columns = list(reversed(COLUMNS))
    path = _mapping(tmp_path, ",".join(cells.get(column, "") for column in columns), header=",".join(columns))

    mapping = read_mapping(str(path))

    assert mapping.problems == []
    assert [(str(row.student_id), row.rate) for row in mapping.rows] == [(STUDENT, (date(2026, 1, 1), 3200))]


def test_the_file_must_be_utf_8_and_may_start_with_a_byte_order_mark(tmp_path: Path):
    path = tmp_path / "mapping.csv"
    path.write_bytes(f"{HEADER}\n{_row(student_party_id=STUDENT, sevdesk_contact_id='12')}\n".encode("utf-8-sig"))
    assert read_mapping(str(path)).count == 1

    path.write_bytes(f"{HEADER}\n".encode() + "Schüler".encode("latin-1") + b"\n")
    with pytest.raises(MappingRefusedError, match="^the file is not UTF-8$"):
        read_mapping(str(path))


def test_a_file_that_is_no_well_formed_csv_is_refused(tmp_path: Path):
    path = _mapping(tmp_path, f'"{STUDENT}"x,,,,,')

    with pytest.raises(MappingRefusedError, match="^the file is no well-formed CSV$"):
        read_mapping(str(path))


@pytest.mark.parametrize(
    ("column", "cell"),
    [
        ("student_party_id", "{5b3c2a10-0000-4000-8000-00000000000b}"),
        ("student_party_id", "5b3c2a1000004000800000000000000b"),
        ("student_party_id", "Student B"),
        ("payer_party_id", f" {PAYER}"),
        ("sevdesk_contact_id", " 4711"),
        ("sevdesk_contact_id", "C-4711"),
        ("sevdesk_contact_id", "1" * 65),
        ("sevdesk_contact_id", "0047"),  # not canonical: "47" and "0047" would be two keys for one contact
        ("sevdesk_contact_id", "0"),
        ("manual_billing", "TRUE"),
        ("manual_billing", "yes"),
        ("manual_billing", "1"),
        ("rate_cents_per_hour", "30.00"),
        ("rate_cents_per_hour", "-4"),
        ("rate_cents_per_hour", str(2**31)),
        ("rate_valid_from", "01.03.2026"),
        ("rate_valid_from", "20260301"),
        ("rate_valid_from", "2026-02-30"),
    ],
)
def test_a_cell_in_another_form_is_malformed(tmp_path: Path, column: str, cell: str):
    cells = {"student_party_id": STUDENT, "payer_party_id": PAYER, "sevdesk_contact_id": "4711"}
    cells |= {"manual_billing": "true", "rate_cents_per_hour": "3200", "rate_valid_from": "2026-03-01"}
    path = _mapping(tmp_path, _row(**cells | {column: cell}))

    mapping = read_mapping(str(path))

    assert (mapping.rows, mapping.problems, mapping.count) == ([], [Problem(2, Reason.MALFORMED_CELL, column)], 1)


def test_the_forms_of_a_well_formed_row(tmp_path: Path):
    path = _mapping(
        tmp_path,
        _row(student_party_id=STUDENT.upper(), payer_party_id=PAYER, sevdesk_contact_id="47", manual_billing="false"),
        _row(student_party_id=STUDENT, rate_cents_per_hour="0", rate_valid_from="2026-03-01"),
    )

    first, second = read_mapping(str(path)).rows

    assert (str(first.student_id), str(first.payer_id), first.contact_id) == (STUDENT, PAYER, "47")
    assert (first.manual_billing, first.rate) == (False, None)
    assert (second.payer_id, second.contact_id, second.manual_billing) == (None, None, None)
    assert second.rate == (date(2026, 3, 1), 0)


def test_row_rules_and_line_numbers(tmp_path: Path):
    """A blank line is skipped but counted as a line; every problem of a row is reported."""
    path = _mapping(
        tmp_path,
        _row(student_party_id=STUDENT),
        "",
        _row(student_party_id=STUDENT, rate_cents_per_hour="3200"),
        _row(student_party_id=STUDENT, rate_valid_from="2026-03-01"),
        _row(student_party_id=STUDENT, sevdesk_contact_id="4711"),
        _row(student_party_id=STUDENT, manual_billing="true"),
        _row(payer_party_id=PAYER),
        f"{STUDENT},,",
        _row(student_party_id="x", rate_cents_per_hour="3200"),
    )

    mapping = read_mapping(str(path))

    assert mapping.count == 8
    assert [row.line for row in mapping.rows] == [2]
    assert [str(problem) for problem in mapping.problems] == [
        "line 4: incomplete_rate",
        "line 5: incomplete_rate",
        "line 6: needs_payer",
        "line 7: needs_payer",
        "line 8: malformed_cell (student_party_id)",
        "line 9: malformed_row",
        "line 10: malformed_cell (student_party_id)",
        "line 10: incomplete_rate",
    ]


# --- The command ------------------------------------------------------------------------------------------------


def test_the_command_is_a_dry_run_unless_apply_is_given():
    parser = billing_backfill.build_parser()

    assert parser.parse_args(["/outside/mapping.csv"]).apply is False
    assert parser.parse_args(["/outside/mapping.csv", "--apply"]).apply is True


def test_main_runs_the_command(monkeypatch):
    calls = []

    async def run(file: str, *, apply: bool) -> None:
        calls.append((file, apply))

    monkeypatch.setattr(billing_backfill, "billing_backfill", run)
    monkeypatch.setattr("sys.argv", ["billing_backfill", "/outside/confirmed mapping.csv", "--apply"])

    billing_backfill.main()

    assert calls == [("/outside/confirmed mapping.csv", True)]


def test_the_just_recipe_hands_its_arguments_on_as_quoted():
    """`{{ args }}` would split a path with whitespace."""
    assert (
        '[positional-arguments]\nbilling-backfill *args:\n    uv run python -m app.cli.billing_backfill "$@"\n'
        in (REPO_ROOT / "justfile").read_text()
    )
