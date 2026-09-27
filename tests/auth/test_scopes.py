from itertools import chain, combinations

import pytest

from app.core.auth import Scope
from app.core.auth.scopes import CLIENT_ONLY_SCOPES, OWN_VARIANT, canonical, expand, format_scopes, parse_scopes


@pytest.mark.parametrize("scope", list(Scope))
def test_scope_carries_a_description(scope: Scope):
    assert scope.description.strip()


def test_scope_value_stays_the_wire_format():
    assert Scope.AUTH_CLIENTS_MANAGE.value == "auth:clients:manage"
    assert f"{Scope.AUTH_CLIENTS_MANAGE}" == "auth:clients:manage"
    assert Scope.AUTH_CLIENTS_MANAGE == "auth:clients:manage"


def test_scope_lookup_by_value_returns_the_member():
    assert Scope("auth:clients:manage") is Scope.AUTH_CLIENTS_MANAGE


def test_no_scope_of_the_bot_is_left():
    """The bot scopes retired with the bot schema (bot-decoupling spec, P0-5)."""
    assert [scope for scope in Scope if scope.startswith("bot:")] == []


def test_client_only_scopes_are_exactly_the_login_scope():
    assert CLIENT_ONLY_SCOPES == {Scope.AUTH_USERS_LOGIN}


def test_own_variant_maps_crm_read_to_crm_read_own_only():
    assert OWN_VARIANT == {Scope.CRM_READ: Scope.CRM_READ_OWN}


def test_expand_adds_the_own_variant_of_an_unqualified_scope():
    assert expand({"crm:read"}) == {"crm:read", "crm:read:own"}


def test_expand_leaves_scopes_without_an_own_variant_and_qualified_scopes_alone():
    assert expand({"auth:clients:manage", "crm:read:own"}) == {"auth:clients:manage", "crm:read:own"}


def test_canonical_drops_the_own_variant_only_when_its_unqualified_scope_is_present():
    assert canonical({"crm:read", "crm:read:own"}) == {"crm:read"}
    assert canonical({"crm:read:own"}) == {"crm:read:own"}


def test_canonical_and_expand_invert_each_other_on_every_subset_of_scope():
    members = list(Scope)
    subsets = chain.from_iterable(combinations(members, size) for size in range(len(members) + 1))

    for subset in map(frozenset, subsets):
        assert canonical(expand(subset)) == canonical(subset)
        assert expand(canonical(subset)) == expand(subset)


def test_parse_scopes_splits_a_scope_string_at_whitespace():
    assert parse_scopes(" auth:clients:manage\tcrm:read  ") == {"auth:clients:manage", "crm:read"}


def test_parse_scopes_takes_an_iterable_value_by_value_and_drops_empty_values():
    """A ``str`` is split, anything else iterated - one scope is never taken apart character by character."""
    assert parse_scopes([Scope.AUTH_CLIENTS_MANAGE, " crm:read ", "", "  "]) == {"auth:clients:manage", "crm:read"}


def test_parse_scopes_reads_none_as_no_scope():
    assert parse_scopes(None) == frozenset()


def test_format_scopes_sorts_and_joins_and_parse_scopes_reads_it_back():
    assert format_scopes({"crm:read", "auth:clients:manage"}) == "auth:clients:manage crm:read"
    assert parse_scopes(format_scopes({"crm:read", "auth:clients:manage"})) == {"auth:clients:manage", "crm:read"}
