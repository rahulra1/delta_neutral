"""Shared expiry-type resolver.

Maps a logical expiry choice — weekly / next_weekly / monthly / next_monthly —
onto a concrete tradable expiry date, given the list of available expiries from
whichever venue the strategy trades (Delta Exchange, NSE, or Groww).

All dates are handled in the project-standard DD-MM-YYYY string format, matching
api.chain.get_expiries / api.nse.get_nse_expiries / api.groww.get_groww_expiries.

Classification is structural: an expiry is "monthly" if it is the last available
expiry within its own calendar month. This works across venues without assuming
a fixed expiry weekday.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

EXPIRY_TYPES = ('daily', 'next_daily', 'weekly', 'next_weekly', 'monthly', 'next_monthly')
_FMT = '%d-%m-%Y'


def _parse(d):
    try:
        return datetime.strptime(d, _FMT).date()
    except (ValueError, TypeError):
        return None


def _sorted_future(expiries, today=None):
    """Return (date, str) pairs, parsed + sorted ascending, today-or-later only."""
    today = today or datetime.now().date()
    out = []
    for e in expiries or []:
        dt = _parse(e)
        if dt and dt >= today:
            out.append((dt, e))
    out.sort(key=lambda x: x[0])
    return out


def _weekly_expiries(future):
    """From sorted (date, str) pairs, return the WEEKLY expiries — the last
    expiry within each ISO (year, week), in chronological order. For a daily
    list this yields the end-of-week expiry; for an already-weekly list each
    expiry is its own week, so the list is unchanged."""
    last_in_week = {}
    for dt, s in future:
        iso = dt.isocalendar()
        last_in_week[(iso[0], iso[1])] = (dt, s)
    return [last_in_week[k] for k in sorted(last_in_week.keys())]


def _monthly_expiries(future):
    """From sorted (date, str) pairs, return the subset that are MONTHLY — the
    last expiry within each calendar (year, month), in chronological order."""
    last_in_month = {}
    for dt, s in future:
        last_in_month[(dt.year, dt.month)] = (dt, s)
    return [last_in_month[k] for k in sorted(last_in_month.keys())]


def resolve_expiry(expiry_type, expiries, today=None):
    """Resolve a logical expiry_type to a concrete DD-MM-YYYY string.

    Args:
        expiry_type: one of EXPIRY_TYPES, or a literal DD-MM-YYYY already in the
                     list (returned as-is), else None if not found.
        expiries:    list of available expiry strings (DD-MM-YYYY).
        today:       optional date override for testing.

    Returns:
        A DD-MM-YYYY string, or None if the list can't satisfy the request.
    """
    if not expiries:
        return None

    if expiry_type not in EXPIRY_TYPES:
        return expiry_type if expiry_type in expiries else None

    future = _sorted_future(expiries, today)
    if not future:
        return None

    if expiry_type == 'daily':
        return future[0][1]
    if expiry_type == 'next_daily':
        return future[1][1] if len(future) >= 2 else future[-1][1]

    weekly = _weekly_expiries(future)
    if expiry_type == 'weekly':
        return weekly[0][1] if weekly else future[0][1]
    if expiry_type == 'next_weekly':
        if len(weekly) >= 2:
            return weekly[1][1]
        return weekly[-1][1] if weekly else future[-1][1]

    monthly = _monthly_expiries(future)
    if not monthly:
        return None
    if expiry_type == 'monthly':
        return monthly[0][1]
    if expiry_type == 'next_monthly':
        return monthly[1][1] if len(monthly) >= 2 else monthly[-1][1]
    return None


def classify_expiries(expiries, today=None):
    """Annotate an expiry list for UI display. Returns [{date, type}] where type
    is 'monthly' (last expiry of its calendar month), else 'weekly' (last expiry
    of its ISO week), else 'daily'."""
    future = _sorted_future(expiries, today)
    monthly_strs = {s for _, s in _monthly_expiries(future)}
    weekly_strs = {s for _, s in _weekly_expiries(future)}
    out = []
    for _, s in future:
        if s in monthly_strs:
            t = 'monthly'
        elif s in weekly_strs:
            t = 'weekly'
        else:
            t = 'daily'
        out.append({'date': s, 'type': t})
    return out
