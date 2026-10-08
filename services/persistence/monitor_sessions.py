# -*- coding: utf-8 -*-
"""Persist RTSP monitor sessions for safety history."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services.common.datetime_format import format_dt
from services.persistence.database import SessionLocal, db_available
from services.persistence.models.monitor_session import (
    SOURCE_ISAPI,
    SOURCE_LOCAL,
    SOURCE_RTSP,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_STREAMING,
    MonitorSession,
)
from services.persistence.result_store import (
    load_session_result,
    rebuild_result_from_crops,
    resolve_session_result,
    result_summary,
    save_session_result,
)


def _live_task(task_id: str):
    from services.tasks.task_manager import get_task

    return get_task(task_id)

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    STATUS_STREAMING: "监控中",
    STATUS_STOPPED: "已停止",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_PENDING: "等待中",
    STATUS_RUNNING: "分析中",
    STATUS_DOWNLOADING: "下载中",
}

_SOURCE_LABELS = {
    SOURCE_RTSP: "实时监控",
    SOURCE_LOCAL: "本地上传",
    SOURCE_ISAPI: "录像回放",
}


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def source_type_label(source_type: str) -> str:
    return _SOURCE_LABELS.get(source_type or SOURCE_RTSP, source_type or SOURCE_RTSP)


def _session_counts(result: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    if not result:
        return 0, 0
    return int(result.get("saved_count") or 0), int(result.get("violation_frames") or 0)


def session_to_dict(row: MonitorSession, *, saved_count: int = 0, violation_frames: int = 0) -> dict:
    return {
        "id": row.id,
        "device_id": row.device_id,
        "device_name": row.device_name,
        "source_type": row.source_type or SOURCE_RTSP,
        "source_type_label": source_type_label(row.source_type or SOURCE_RTSP),
        "task_id": row.task_id,
        "status": row.status,
        "status_label": status_label(row.status),
        "frame_stride": row.frame_stride,
        "enable_reid": bool(row.enable_reid),
        "reid_threshold": float(row.reid_threshold),
        "output_dir": row.output_dir,
        "error_message": row.error_message,
        "started_at": format_dt(row.started_at),
        "stopped_at": format_dt(row.stopped_at),
        "saved_count": saved_count,
        "violation_frames": violation_frames,
    }


def list_sessions(
    db: Session,
    *,
    device_id: Optional[int] = None,
    source_type: Optional[str] = None,
    status: Optional[str] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[dict], int]:
    q = db.query(MonitorSession)
    if device_id is not None:
        q = q.filter(MonitorSession.device_id == device_id)
    if source_type:
        q = q.filter(MonitorSession.source_type == source_type)
    if status:
        q = q.filter(MonitorSession.status == status)
    if started_from is not None:
        q = q.filter(MonitorSession.started_at >= started_from)
    if started_to is not None:
        q = q.filter(MonitorSession.started_at <= started_to)
    total = q.count()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    rows = (
        q.order_by(MonitorSession.started_at.desc(), MonitorSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for row in rows:
        live = _live_task(row.task_id)
        if live and live.get("result"):
            saved_count, violation_frames = _session_counts(live["result"])
        else:
            saved_count, violation_frames = result_summary(row.output_dir)
        items.append(session_to_dict(row, saved_count=saved_count, violation_frames=violation_frames))
    return items, total


def get_session(db: Session, session_id: int) -> Optional[MonitorSession]:
    return db.query(MonitorSession).filter(MonitorSession.id == session_id).first()


def get_session_detail(db: Session, session_id: int) -> Optional[dict]:
    row = get_session(db, session_id)
    if not row:
        return None
    live = _live_task(row.task_id)
    live_result = live.get("result") if live else None
    result = resolve_session_result(
        row.task_id,
        row.output_dir,
        frame_stride=row.frame_stride,
        enable_reid=bool(row.enable_reid),
        reid_threshold=float(row.reid_threshold),
        live_result=live_result,
    )
    saved_count, violation_frames = _session_counts(result)
    session = session_to_dict(row, saved_count=saved_count, violation_frames=violation_frames)
    task_status = live.get("status") if live else row.status
    task_error = row.error_message if row.status == STATUS_FAILED else None
    task_message = (live or {}).get("message") or session["status_label"]
    if row.error_message and row.status != STATUS_FAILED:
        task_message = row.error_message
    return {
        "session": session,
        "task": {
            "id": row.task_id,
            "status": task_status,
            "message": task_message,
            "video_name": row.device_name,
            "source_type": row.source_type or SOURCE_RTSP,
            "device_id": row.device_id,
            "result": result,
            "created_at": session["started_at"],
            "finished_at": session["stopped_at"],
            "error": task_error,
        },
    }


def record_session_start(
    *,
    task_id: str,
    output_dir: str,
    device_id: Optional[int] = None,
    device_name: str = "",
    source_type: str = SOURCE_RTSP,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
) -> None:
    if not db_available() or SessionLocal is None:
        return
    db = SessionLocal()
    try:
        initial_status = STATUS_STREAMING if source_type == SOURCE_RTSP else STATUS_PENDING
        row = MonitorSession(
            device_id=device_id,
            task_id=task_id,
            device_name=(device_name or "").strip() or task_id,
            source_type=source_type,
            status=initial_status,
            frame_stride=max(1, int(frame_stride)),
            enable_reid=1 if enable_reid else 0,
            reid_threshold=round(float(reid_threshold), 2),
            output_dir=output_dir,
            started_at=datetime.now(),
        )
        db.add(row)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("monitor session start record failed task_id=%s: %s", task_id, e)
    finally:
        db.close()


def upsert_analysis_record(
    *,
    task_id: str,
    source_type: str,
    output_dir: str,
    device_name: str,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
    status: str = STATUS_PENDING,
) -> None:
    """Create or reset safety record for local/isapi task (same task_id may re-run)."""
    if not db_available() or SessionLocal is None:
        return
    if source_type not in (SOURCE_LOCAL, SOURCE_ISAPI):
        return
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        name = (device_name or "").strip() or task_id
        if row:
            row.source_type = source_type
            row.device_name = name
            row.device_id = None
            row.status = status
            row.frame_stride = max(1, int(frame_stride))
            row.enable_reid = 1 if enable_reid else 0
            row.reid_threshold = round(float(reid_threshold), 2)
            row.output_dir = output_dir
            row.error_message = None
            row.started_at = datetime.now()
            row.stopped_at = None
        else:
            db.add(
                MonitorSession(
                    device_id=None,
                    task_id=task_id,
                    device_name=name,
                    source_type=source_type,
                    status=status,
                    frame_stride=max(1, int(frame_stride)),
                    enable_reid=1 if enable_reid else 0,
                    reid_threshold=round(float(reid_threshold), 2),
                    output_dir=output_dir,
                    started_at=datetime.now(),
                )
            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("upsert analysis record failed task_id=%s: %s", task_id, e)
    finally:
        db.close()


def stop_analysis_session(
    task_id: str,
    *,
    result: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    if not db_available() or SessionLocal is None:
        return
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        if not row:
            return
        if result:
            save_session_result(row.output_dir, result)
        row.status = STATUS_STOPPED
        row.stopped_at = datetime.now()
        row.error_message = (reason or "")[:2000] or None
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("stop analysis session failed task_id=%s: %s", task_id, e)
    finally:
        db.close()


def stop_stale_analysis_session(
    task_id: str,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Stop analysis record in DB when worker is no longer in memory."""
    if not db_available() or SessionLocal is None:
        return False
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        if not row:
            return False
        if row.source_type not in (SOURCE_LOCAL, SOURCE_ISAPI):
            return False
        if row.status not in (STATUS_PENDING, STATUS_DOWNLOADING, STATUS_RUNNING):
            return False
        result = load_session_result(row.output_dir)
        if not result:
            result = rebuild_result_from_crops(
                task_id,
                row.output_dir,
                frame_stride=row.frame_stride,
                enable_reid=bool(row.enable_reid),
                reid_threshold=float(row.reid_threshold),
            )
        stop_analysis_session(task_id, result=result, reason=reason)
        return True
    except Exception as e:
        db.rollback()
        logger.warning("stop stale analysis session failed task_id=%s: %s", task_id, e)
        return False
    finally:
        db.close()


def stop_stale_streaming_session(
    task_id: str,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Stop RTSP session in DB when live thread is no longer running."""
    if not db_available() or SessionLocal is None:
        return False
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        if not row:
            return False
        if row.source_type != SOURCE_RTSP:
            return False
        if row.status not in (STATUS_STREAMING, STATUS_PENDING):
            return False
        result = load_session_result(row.output_dir)
        if not result:
            result = rebuild_result_from_crops(
                task_id,
                row.output_dir,
                frame_stride=row.frame_stride,
                enable_reid=bool(row.enable_reid),
                reid_threshold=float(row.reid_threshold),
            )
        if result:
            save_session_result(row.output_dir, result)
        row.status = STATUS_STOPPED
        row.stopped_at = datetime.now()
        row.error_message = (reason or "")[:2000] or None
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning("stop stale streaming session failed task_id=%s: %s", task_id, e)
        return False
    finally:
        db.close()


def finish_analysis_session(
    task_id: str,
    *,
    success: bool,
    result: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if not db_available() or SessionLocal is None:
        return
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        if not row:
            return
        if success and result:
            save_session_result(row.output_dir, result)
        row.status = STATUS_COMPLETED if success else STATUS_FAILED
        row.stopped_at = datetime.now()
        if error_message:
            row.error_message = error_message[:2000]
        elif success:
            row.error_message = None
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("finish analysis session failed task_id=%s: %s", task_id, e)
    finally:
        db.close()

def update_session_status(
    task_id: str,
    status: str,
    *,
    error_message: Optional[str] = None,
) -> None:
    if not db_available() or SessionLocal is None:
        return
    db = SessionLocal()
    try:
        row = db.query(MonitorSession).filter(MonitorSession.task_id == task_id).first()
        if not row:
            return
        row.status = status
        if error_message:
            row.error_message = error_message[:2000]
        if status in (STATUS_STOPPED, STATUS_COMPLETED, STATUS_FAILED):
            row.stopped_at = datetime.now()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("monitor session update failed task_id=%s: %s", task_id, e)
    finally:
        db.close()


def list_streaming_task_ids_for_device(device_id: int) -> List[str]:
    if not db_available() or SessionLocal is None:
        return []
    db = SessionLocal()
    try:
        rows = (
            db.query(MonitorSession.task_id)
            .filter(MonitorSession.device_id == device_id, MonitorSession.status == STATUS_STREAMING)
            .all()
        )
        return [r[0] for r in rows]
    finally:
        db.close()


def reconcile_orphan_streaming_sessions() -> int:
    """Mark DB sessions as stopped when no live RTSP thread is running."""
    if not db_available() or SessionLocal is None:
        return 0
    from services.tasks.rtsp_stream import is_rtsp_running

    db = SessionLocal()
    count = 0
    try:
        rows = db.query(MonitorSession).filter(
            MonitorSession.status == STATUS_STREAMING,
            MonitorSession.source_type == SOURCE_RTSP,
        ).all()
        for row in rows:
            if is_rtsp_running(row.task_id):
                continue
            row.status = STATUS_STOPPED
            row.stopped_at = datetime.now()
            if not row.error_message:
                row.error_message = "服务重启或监控已结束"
            count += 1
        if count:
            db.commit()
        return count
    except Exception as e:
        db.rollback()
        logger.warning("reconcile orphan streaming sessions failed: %s", e)
        return 0
    finally:
        db.close()


class SessionDeleteError(RuntimeError):
    """Raised when session files cannot be removed from disk."""


def delete_monitor_session(db: Session, session_id: int) -> None:
    from services.tasks.task_manager import purge_session_artifacts

    row = get_session(db, session_id)
    if not row:
        raise LookupError("记录不存在")
    try:
        purge_session_artifacts(
            row.task_id,
            output_dir=row.output_dir,
            video_name=row.device_name,
            source_type=row.source_type,
        )
    except RuntimeError as e:
        raise SessionDeleteError(str(e)) from e
    db.delete(row)
    db.commit()
