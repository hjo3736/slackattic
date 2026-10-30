from decimal import Decimal

import pytest

from slackattic.core.timestamps import format_ts, month_of, parse_ts, shift_ts


def test_parse_keeps_microseconds_exactly():
    assert parse_ts("1793344750.472099") == Decimal("1793344750.472099")


@pytest.mark.parametrize("bad", [None, 1.5, "", "abc", "-1.0", "NaN", "Infinity"])
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_ts(bad)


def test_shift_keeps_six_decimals():
    assert shift_ts("1793344750.472099", -86400) == "1793258350.472099"
    assert shift_ts("10.000001", -100) == "0.000000"
    assert format_ts(Decimal("5")) == "5.000000"


@pytest.mark.parametrize(
    ("ts", "month"),
    [
        # 2026-10-31 23:59:59 UTC is already November in KST, but files use UTC
        ("1793491199.000000", "2026-10"),
        ("1793491200.000000", "2026-11"),
    ],
)
def test_month_uses_utc(ts, month):
    assert month_of(ts) == month
