# -*- coding: utf-8 -*-
"""ORM model for monitoring devices."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from services.persistence.database import Base

# status: 0 unknown, 1 online, 2 offline
STATUS_UNKNOWN = 0
STATUS_ONLINE = 1
STATUS_OFFLINE = 2


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    rtsp_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=STATUS_UNKNOWN)
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="admin")
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
