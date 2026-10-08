# -*- coding: utf-8 -*-
"""Datetime helpers (MariaDB 5.5 may return DATETIME as str)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Union

_DtLike = Union[datetime, date, str]


def _normalize_dt_str(s: str) -> str:
    if "T" in s:
        s = s.replace("T", " ", 1)
    if "." in s:
        s = s.split(".", 1)[0]
    return s


def format_dt(value: Optional[_DtLike]) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("0000-00-00"):
            return None
        return _normalize_dt_str(s)
    if value.year <= 1:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value.isoformat()
