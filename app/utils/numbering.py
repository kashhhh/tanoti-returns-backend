"""Random request identifiers avoid count-based collisions across workers."""
from uuid import uuid4


def next_return_number():
    return "RET-" + uuid4().hex[:16].upper()


def next_exchange_number():
    return "EXC-" + uuid4().hex[:16].upper()
