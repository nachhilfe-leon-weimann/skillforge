"""`just seed-demo` writes only to a local database, and refuses before anything connects (billing spec, SF2-3).

Local is `localhost`, a loopback address or a single-label host name such as a compose service. Everything the spec
does not name fails closed: an address that is not loopback, a label the resolver reads as an address, no host,
several hosts, a query parameter, a URL that cannot be parsed.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import seed_demo
from app.cli.seed_demo import HostClass, NotLocalError, local_host_class

PASSWORD = "secret-pw-4711"
JUSTFILE = Path(__file__).resolve().parents[1] / "justfile"


def _url(host: str) -> str:
    return f"postgresql+asyncpg://demo:{PASSWORD}@{host}/skillforge"


def test_localhost_is_accepted():
    assert local_host_class(_url("localhost:5432")) is HostClass.LOCALHOST
    assert local_host_class(_url("LocalHost")) is HostClass.LOCALHOST


@pytest.mark.parametrize("host", ["127.0.0.1:5432", "127.0.0.53", "[::1]:5432", "[::1]"])
def test_a_loopback_address_is_accepted(host: str):
    assert local_host_class(_url(host)) is HostClass.LOOPBACK


@pytest.mark.parametrize("host", ["db:5432", "postgres-17", "skillforge_db"])
def test_a_compose_host_name_is_accepted(host: str):
    assert local_host_class(_url(host)) is HostClass.SINGLE_LABEL


@pytest.mark.parametrize("host", ["db.example.org:5432", "localhost.example.org", "localhost.", "db.local"])
def test_a_fully_qualified_host_name_is_refused(host: str):
    with pytest.raises(NotLocalError, match="^the host is a fully qualified name$"):
        local_host_class(_url(host))


@pytest.mark.parametrize(
    "host", ["192.0.2.10", "10.0.0.5:5432", "0.0.0.0", "[2001:db8::1]:5432", "[::]", "[::ffff:192.0.2.10]"]
)
def test_an_address_that_is_not_loopback_is_refused(host: str):
    with pytest.raises(NotLocalError, match="^the host is an address that is not loopback$"):
        local_host_class(_url(host))


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "1"])
def test_a_label_the_resolver_reads_as_an_address_is_refused(host: str):
    """The C resolver reads these as IPv4 addresses (127.0.0.1, 0.0.0.1): an address in disguise, not a name."""
    with pytest.raises(NotLocalError, match="^the host is no plain host name$"):
        local_host_class(_url(host))


@pytest.mark.parametrize("host", ["db\u212a", "-db", "db-"])
def test_a_label_outside_the_plain_ascii_name_is_refused(host: str):
    """Case-insensitive Unicode matching would read the Kelvin sign as a `k`; a name is plain ASCII or refused."""
    with pytest.raises(NotLocalError, match="^the host is no plain host name$"):
        local_host_class(_url(host))


@pytest.mark.parametrize("url", ["postgresql+asyncpg://demo@/skillforge", "postgresql+asyncpg://demo@:5432/skillforge"])
def test_a_url_without_a_host_is_refused(url: str):
    """Without a host the driver falls back to its environment (`PGHOST`), which the guard cannot see."""
    with pytest.raises(NotLocalError, match="^the database URL names no host$"):
        local_host_class(url)


@pytest.mark.parametrize("url", ["", "not a url", _url("db:5432,db.example.org:5432"), _url("db:port")])
def test_a_url_that_cannot_be_parsed_is_refused(url: str):
    with pytest.raises(NotLocalError, match="^the database URL cannot be parsed$"):
        local_host_class(url)


@pytest.mark.parametrize(
    "scheme",
    ["postgresql", "postgres", "postgresql+psycopg", "postgresql+psycopg2", "mysql+aiomysql", "sqlite+aiosqlite"],
)
def test_another_driver_than_the_apps_is_refused(scheme: str):
    """A libpq driver reads `PGHOSTADDR` and `PGSERVICE` from the environment, past a `localhost` in the URL."""
    with pytest.raises(NotLocalError, match=r"^the database URL does not use the driver postgresql\+asyncpg$"):
        local_host_class(f"{scheme}://demo:{PASSWORD}@localhost:5432/skillforge")


@pytest.mark.parametrize("query", ["?host=db.example.org", "?ssl=disable"])
def test_a_url_with_query_parameters_is_refused(query: str):
    """The driver takes `host` from the query over the one before the path."""
    with pytest.raises(NotLocalError, match="^the database URL carries query parameters$"):
        local_host_class(_url("localhost") + query)


async def test_a_refused_host_opens_no_connection(monkeypatch, capsys):
    opened = _record_connections(monkeypatch, _url("db.example.org:5432"))

    with pytest.raises(SystemExit) as refusal:
        await seed_demo.seed_demo()

    assert opened == []
    assert str(refusal.value.code) == (
        "seed-demo refused: the host is a fully qualified name; it writes only to a local database"
    )
    assert capsys.readouterr().out == ""


async def test_the_command_checks_the_url_it_connects_with_and_prints_no_part_of_it(monkeypatch, capsys):
    url = _url("db:5432")
    opened = _record_connections(monkeypatch, url)

    with pytest.raises(_Connected):
        await seed_demo.seed_demo()

    assert opened == [url]
    output = capsys.readouterr().out
    assert output == "database host: single-label host name\n"
    assert PASSWORD not in output


class _Connected(Exception):
    """Raised where the command would open its engine."""


def _record_connections(monkeypatch, url: str) -> list[str]:
    """Configure ``url`` and record every engine the command opens - through ``Database`` or around it."""
    opened: list[str] = []

    def from_url(cls, given: str, **kwargs):
        opened.append(given)
        raise _Connected

    def create_async_engine(given, *args, **kwargs):
        opened.append(str(given))
        raise _Connected

    monkeypatch.setattr(seed_demo, "get_settings", lambda: SimpleNamespace(db=SimpleNamespace(url=url)))
    monkeypatch.setattr(seed_demo.Database, "from_url", classmethod(from_url))
    monkeypatch.setattr("app.core.db.database.create_async_engine", create_async_engine)
    return opened


def test_the_recipe_runs_the_command_and_nothing_else():
    assert "\nseed-demo:\n    uv run python -m app.cli.seed_demo\n" in JUSTFILE.read_text()
