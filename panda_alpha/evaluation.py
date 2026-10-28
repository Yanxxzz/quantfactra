"""Generic time-window validation, without data sources or strategy libraries."""
from datetime import date

def check_window(start, end, sealed, warmup_start=None):
    first, last = date.fromisoformat(str(warmup_start or start)[:10]), date.fromisoformat(str(end)[:10])
    decision = date.fromisoformat(str(start)[:10])
    if first > decision or decision > last:
        raise ValueError('Invalid research interval')
    for window in sealed:
        if first <= date.fromisoformat(window['end'][:10]) and last >= date.fromisoformat(window['start'][:10]):
            raise ValueError('Research/warmup interval overlaps sealed OOS')
