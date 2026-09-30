"""Kept so that ``stockroom.dates`` still imports; the code lives in ``stockroom.util.dates``."""

from __future__ import annotations

from .util.dates import add_months, days_between, days_in_month, format_date, month_key, months_between, parse_date

__all__ = ["add_months", "days_between", "days_in_month", "format_date", "month_key", "months_between", "parse_date"]
