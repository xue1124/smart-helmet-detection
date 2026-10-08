# -*- coding: utf-8 -*-
"""CRUD and helpers for monitoring devices."""

from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any, List, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from services.common.datetime_format import format_dt
from services.persistence.models.device import STATUS_OFFLINE, STATUS_ONLINE, STATUS_UNKNOWN, Device

_RTSP_RE = re.compile(r"^rtsp://", re.I)
_UNSET: Any = object()


def mask_rtsp_url(url: str) -> str:
    """Hide password in RTSP URL for API responses."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.password:
            host_part = parsed.hostname or ""
            if parsed.port:
                host_part = f"{host_part}:{parsed.port}"
            user = parsed.username or ""
            auth = f"{user}:***@" if user else ""
            path = parsed.path or ""
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return f"rtsp://{auth}{host_part}{path}"
    except Exception:
        pass
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", url)


def device_to_dict(device: Device, *, mask_url: bool = True) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "location": device.location or "",
        "rtsp_url": mask_rtsp_url(device.rtsp_url) if mask_url else device.rtsp_url,
        "status": device.status,
        "status_label": status_label(device.status),
        "enabled": bool(device.enabled),
        "created_by": device.created_by or "admin",
        "remark": device.remark or "",
        "last_probe_at": format_dt(device.last_probe_at),
        "created_at": format_dt(device.created_at),
        "updated_at": format_dt(device.updated_at),
    }


def status_label(status: int) -> str:
    return {STATUS_UNKNOWN: "未知", STATUS_ONLINE: "在线", STATUS_OFFLINE: "离线"}.get(
        status, "未知"
    )


def validate_rtsp_url(url: str) -> str:
    u = (url or "").strip()
    if not _RTSP_RE.match(u):
        raise ValueError("RTSP 地址必须以 rtsp:// 开头")
    return u


def list_devices(
    db: Session,
    *,
    name: Optional[str] = None,
    location: Optional[str] = None,
    status: Optional[int] = None,
    enabled_only: bool = False,
) -> List[Device]:
    q = db.query(Device).order_by(Device.id.desc())
    if enabled_only:
        q = q.filter(Device.enabled == 1)
    if name:
        q = q.filter(Device.name.like(f"%{name.strip()}%"))
    if location:
        q = q.filter(Device.location.like(f"%{location.strip()}%"))
    if status is not None:
        q = q.filter(Device.status == status)
    return q.all()


def get_device(db: Session, device_id: int) -> Optional[Device]:
    return db.query(Device).filter(Device.id == device_id).first()


def create_device(
    db: Session,
    *,
    name: str,
    rtsp_url: str,
    location: Optional[str] = None,
    enabled: bool = True,
    remark: Optional[str] = None,
    created_by: str = "admin",
) -> Device:
    n = (name or "").strip()
    if not n:
        raise ValueError("设备名称不能为空")
    url = validate_rtsp_url(rtsp_url)
    now = datetime.now()
    device = Device(
        name=n,
        location=(location or "").strip() or None,
        rtsp_url=url,
        enabled=1 if enabled else 0,
        remark=(remark or "").strip() or None,
        created_by=created_by or "admin",
        status=STATUS_UNKNOWN,
        created_at=now,
        updated_at=now,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def update_device(
    db: Session,
    device_id: int,
    *,
    name: Any = _UNSET,
    rtsp_url: Any = _UNSET,
    location: Any = _UNSET,
    enabled: Any = _UNSET,
    remark: Any = _UNSET,
) -> Device:
    device = get_device(db, device_id)
    if not device:
        raise LookupError("设备不存在")
    if name is not _UNSET:
        n = (name or "").strip()
        if not n:
            raise ValueError("设备名称不能为空")
        device.name = n
    if rtsp_url is not _UNSET:
        raw = (rtsp_url or "").strip()
        if "***" not in raw:
            device.rtsp_url = validate_rtsp_url(raw)
    if location is not _UNSET:
        device.location = (location or "").strip() or None
    if enabled is not _UNSET:
        device.enabled = 1 if enabled else 0
    if remark is not _UNSET:
        device.remark = (remark or "").strip() or None
    device.updated_at = datetime.now()
    db.commit()
    db.refresh(device)
    return device


def delete_device(db: Session, device_id: int) -> None:
    device = get_device(db, device_id)
    if not device:
        raise LookupError("设备不存在")
    db.delete(device)
    db.commit()


def probe_rtsp_url(rtsp_url: str, timeout_sec: float = 8.0) -> bool:
    """Try opening RTSP stream briefly."""
    import cv2

    box: dict = {"ok": False}

    def _worker() -> None:
        cap = None
        try:
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            for prop, val in (
                (getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None), int(timeout_sec * 1000)),
                (getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None), int(timeout_sec * 1000)),
            ):
                if prop is not None:
                    try:
                        cap.set(prop, val)
                    except Exception:
                        pass
            if cap.isOpened():
                ok, _ = cap.read()
                box["ok"] = bool(ok)
        except Exception:
            box["ok"] = False
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec + 2)
    return bool(box["ok"])


def probe_device(db: Session, device_id: int) -> Device:
    device = get_device(db, device_id)
    if not device:
        raise LookupError("设备不存在")
    online = probe_rtsp_url(device.rtsp_url)
    device.status = STATUS_ONLINE if online else STATUS_OFFLINE
    now = datetime.now()
    device.last_probe_at = now
    device.updated_at = now
    db.commit()
    db.refresh(device)
    return device
