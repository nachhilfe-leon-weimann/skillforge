"""Billing settings: nested under ``BILLING__``; the default rate obeys rule G itself (billing spec, decision G)."""

import pytest
from pydantic import ValidationError

from app.core.config import BillingSettings, Settings


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No ``BILLING__*`` variable, and a working directory without an env file: independent of the checkout."""
    monkeypatch.delenv("BILLING__DEFAULT_RATE_CENTS_PER_HOUR", raising=False)
    monkeypatch.delenv("billing__default_rate_cents_per_hour", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_the_default_rate_is_30_euros_per_hour(clean_env):
    assert Settings(_env_file=None).billing.default_rate_cents_per_hour == 3000


def test_the_default_rate_is_read_under_its_prefix(clean_env):
    clean_env.setenv("BILLING__DEFAULT_RATE_CENTS_PER_HOUR", "3200")

    assert Settings(_env_file=None).billing.default_rate_cents_per_hour == 3200


@pytest.mark.parametrize("cents", [0, 4, 3600])
def test_a_rate_divisible_by_4_is_accepted(clean_env, cents: int):
    assert BillingSettings(_env_file=None, default_rate_cents_per_hour=cents).default_rate_cents_per_hour == cents


@pytest.mark.parametrize("value", ["3001", "3002", "3003", "-4", "30.00"])
def test_a_rate_off_the_quarter_hour_or_below_zero_is_refused(clean_env, value: str):
    clean_env.setenv("BILLING__DEFAULT_RATE_CENTS_PER_HOUR", value)

    with pytest.raises(ValidationError):
        BillingSettings(_env_file=None)
