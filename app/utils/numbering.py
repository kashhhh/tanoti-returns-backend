"""
Simple sequential number generator, e.g. RET-000123 / EXC-000123.
Counts existing rows -- fine at this scale; swap for a DB sequence if
volume ever gets high enough for race conditions to matter.
"""
from app.models import ReturnRequest, ExchangeRequest


def next_return_number() -> str:
    count = ReturnRequest.query.count() + 1
    return f"RET-{count:06d}"


def next_exchange_number() -> str:
    count = ExchangeRequest.query.count() + 1
    return f"EXC-{count:06d}"
