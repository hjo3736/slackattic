from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


def parse_ts(ts: object) -> Decimal:
    """Parse a Slack ts exactly; floats would lose the microsecond part."""
    if not isinstance(ts, str):
        raise ValueError(f"invalid Slack ts: {ts!r}")
    try:
        value = Decimal(ts)
    except InvalidOperation:
        raise ValueError(f"invalid Slack ts: {ts!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"invalid Slack ts: {ts!r}")
    return value


def format_ts(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001'))}"


def shift_ts(ts: str, seconds: int) -> str:
    return format_ts(max(parse_ts(ts) + seconds, Decimal(0)))


def month_of(ts: str) -> str:
    # UTC so every machine splits messages into the same files
    return datetime.fromtimestamp(int(parse_ts(ts)), tz=UTC).strftime("%Y-%m")
