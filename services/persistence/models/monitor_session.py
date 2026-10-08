# -*- coding: utf-8 -*-
"""ORM model for RTSP monitor sessions (safety record source)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Numeric, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from services.persistence.database import Base

STATUS_STREAMING = "streaming"
STATUS_STOPPED = "stopped"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DOWNLOADING = "downloading"

SOURCE_RTSP = "rtsp"
SOURCE_LOCAL = "local"
SOURCE_ISAPI = "isapi"


class MonitorSession(Base):
    __tablename__ = "monitor_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    task_id: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_RTSP, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_STREAMING)
    frame_stride: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=25)
    enable_reid: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    reid_threshold: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=0.70)
    output_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
