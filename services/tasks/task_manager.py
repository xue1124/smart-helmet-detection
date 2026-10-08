# -*- coding: utf-8 -*-
"""In-memory async task manager for video processing."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from services.analysis.helmet_detector import HelmetDetector
from services.integrations.isapi_client import auto_task_name, build_isapi_url, download_video
from services.analysis.video_analyzer import OfflineVideoAnalyzer
from services.config import OUTPUT_DIR, UPLOAD_DIR

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".m4v", ".mpeg", ".mpg"}

_ANALYSIS_ACTIVE_STATUSES = ("pending", "downloading", "running")

_detector: Optional[HelmetDetector] = None
_tasks: Dict[str, Dict] = {}
_lock = threading.Lock()

_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def task_id_from_video_path(video_path: Path) -> str:
    """Task folder name = uploaded video filename (stem), sanitized for Windows."""
    name = video_path.stem.strip() or video_path.name.strip() or "video"
    name = _WIN_INVALID.sub("_", name).rstrip(". ")
    if not name:
        name = "video"
    return name[:200]


def _sanitize_task_id(name: str) -> str:
    name = _WIN_INVALID.sub("_", (name or "").strip()).rstrip(". ")
    return (name or "video")[:200]


def _task_id_from_name(name: str) -> str:
    return _sanitize_task_id(name)


def _rtsp_task_id(base_name: str) -> str:
    """Unique task folder per RTSP start; never reuse so history crops are kept."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = _task_id_from_name(f"{base_name}_{ts}")
    if (OUTPUT_DIR / task_id).exists():
        task_id = _task_id_from_name(f"{base_name}_{ts}_{uuid.uuid4().hex[:6]}")
    return task_id


def _invalidate_task_run(task_id: str) -> None:
    """Mark in-flight worker stale so it stops updating this task_id."""
    with _lock:
        t = _tasks.get(task_id)
        if t:
            t["run_token"] = f"__superseded__{uuid.uuid4()}"


def _list_active_analysis_task_ids(source_type: str = "isapi") -> List[str]:
    with _lock:
        return [
            t["id"]
            for t in _tasks.values()
            if t.get("source_type") == source_type and t.get("status") in _ANALYSIS_ACTIVE_STATUSES
        ]


def stop_analysis_task(task_id: str, *, reason: str = "已停止") -> bool:
    """Stop a local/isapi analysis task and persist partial progress."""
    from services.persistence.monitor_sessions import stop_analysis_session

    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("source_type") not in ("isapi", "local"):
            return False
        if t.get("status") not in _ANALYSIS_ACTIVE_STATUSES:
            return False
        result = t.get("result")
        output_dir = t.get("output_dir")

    _invalidate_task_run(task_id)

    with _lock:
        t = _tasks.get(task_id)
        if t:
            t["status"] = "stopped"
            t["message"] = reason
            t["finished_at"] = datetime.now().isoformat()

    if output_dir and result:
        from services.persistence.result_store import save_session_result

        save_session_result(output_dir, result)
    stop_analysis_session(task_id, result=result, reason=reason)
    return True


def stop_active_isapi_analysis(exclude_task_id: Optional[str] = None) -> List[str]:
    stopped: List[str] = []
    for tid in _list_active_analysis_task_ids("isapi"):
        if exclude_task_id and tid == exclude_task_id:
            continue
        if stop_analysis_task(tid, reason="已停止（新任务已开始）"):
            stopped.append(tid)
    return stopped


def stop_video_analysis(task_id: str, *, reason: str = "用户停止") -> bool:
    """Stop a local or ISAPI analysis task (memory + DB)."""
    tid = (task_id or "").strip()
    if not tid:
        return False
    if stop_analysis_task(tid, reason=reason):
        return True
    from services.persistence.monitor_sessions import stop_stale_analysis_session

    return stop_stale_analysis_session(tid, reason=reason)


def _purge_task_artifacts(task_id: str, keep_video: Optional[Path] = None) -> None:
    """Remove output/upload files for task_id (and legacy _2/_3 variants)."""
    keep_resolved = Path(keep_video).resolve() if keep_video else None

    def _should_remove(path: Path) -> bool:
        if keep_resolved is None:
            return True
        try:
            return path.resolve() != keep_resolved
        except OSError:
            return True

    _remove_output_dir(OUTPUT_DIR / task_id)
    upload = UPLOAD_DIR / f"{task_id}.mp4"
    if upload.exists() and _should_remove(upload):
        try:
            upload.unlink()
        except OSError:
            pass
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.glob(f"{task_id}_*"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.glob(f"{task_id}_*.mp4"):
            if _should_remove(f):
                try:
                    f.unlink()
                except OSError:
                    pass


def _stop_rtsp_and_wait(task_id: str, timeout_sec: float = 10.0) -> None:
    from services.tasks.rtsp_stream import is_rtsp_running, request_stop
    import time

    if not is_rtsp_running(task_id):
        return
    request_stop(task_id)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_rtsp_running(task_id):
            return
        time.sleep(0.05)
    if is_rtsp_running(task_id):
        raise RuntimeError(f"RTSP 任务 {task_id} 停止超时，请稍后再试")


def _remove_output_dir(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)


def _chmod_writable(func, path, _exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _force_remove_tree(path: Path) -> None:
    """Delete a directory tree; raise if it still exists after retries."""
    target = path.resolve()
    if not target.exists():
        return
    last_err: Optional[OSError] = None
    for attempt in range(3):
        try:
            shutil.rmtree(target, onerror=_chmod_writable)
            if not target.exists():
                return
        except OSError as e:
            last_err = e
            logger.warning("remove tree attempt %s failed path=%s: %s", attempt + 1, target, e)
            time.sleep(0.15)
    if target.exists():
        msg = f"无法删除目录: {target}"
        if last_err:
            msg = f"{msg} ({last_err})"
        raise RuntimeError(msg)


def _force_remove_file(path: Path) -> None:
    target = path.resolve()
    if not target.is_file():
        return
    try:
        target.unlink()
    except OSError as e:
        raise RuntimeError(f"无法删除文件: {target} ({e})") from e


def _collect_upload_video_paths(
    task_id: str,
    *,
    video_name: Optional[str] = None,
) -> set[Path]:
    """Resolve candidate upload video paths (local original name / isapi task_id.mp4)."""
    tid = (task_id or "").strip()
    paths: set[Path] = set()

    with _lock:
        live = _tasks.get(tid)
        if live and live.get("video_path"):
            paths.add(Path(live["video_path"]).resolve())

    if video_name:
        paths.add((UPLOAD_DIR / video_name).resolve())

    paths.add((UPLOAD_DIR / f"{tid}.mp4").resolve())

    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if f.stem == tid or (video_name and f.name == video_name):
                paths.add(f.resolve())
        for f in UPLOAD_DIR.glob(f"{tid}_*.mp4"):
            if f.is_file():
                paths.add(f.resolve())

    return paths


def purge_session_artifacts(
    task_id: str,
    output_dir: Optional[str] = None,
    *,
    video_name: Optional[str] = None,
    source_type: Optional[str] = None,
) -> None:
    """Stop live task if needed and remove output/upload files for a session."""
    from services.tasks.rtsp_stream import is_rtsp_running

    tid = (task_id or "").strip()
    if not tid:
        return

    video_paths = _collect_upload_video_paths(tid, video_name=video_name)

    if is_rtsp_running(tid):
        stop_rtsp_task(tid)
        try:
            _stop_rtsp_and_wait(tid, timeout_sec=10.0)
        except RuntimeError:
            pass

    _invalidate_task_run(tid)
    with _lock:
        _tasks.pop(tid, None)

    dirs: set[Path] = set()
    dirs.add((OUTPUT_DIR / tid).resolve())
    if output_dir:
        dirs.add(Path(output_dir).resolve())
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.glob(f"{tid}_*"):
            if d.is_dir():
                dirs.add(d.resolve())

    for d in sorted(dirs, key=lambda p: len(str(p)), reverse=True):
        _force_remove_tree(d)

    for video in sorted(video_paths, key=lambda p: len(str(p)), reverse=True):
        _force_remove_file(video)


def purge_rtsp_task_data(
    task_id: str,
    output_dir: Optional[str] = None,
    *,
    video_name: Optional[str] = None,
    source_type: Optional[str] = None,
) -> None:
    """Backward-compatible alias for session artifact cleanup."""
    purge_session_artifacts(
        task_id,
        output_dir=output_dir,
        video_name=video_name,
        source_type=source_type,
    )


def get_helmet_detector() -> HelmetDetector:
    global _detector
    if _detector is None:
        weights = HelmetDetector.default_weights()
        _detector = HelmetDetector(weights=weights)
    return _detector


def _register_task(task: Dict) -> None:
    with _lock:
        _tasks[task["id"]] = task


def _empty_result(
    frame_stride: int,
    enable_reid: bool,
    reid_threshold: float,
) -> Dict:
    return {
        "saved_count": 0,
        "violation_frames": 0,
        "sampled_frames": 0,
        "processed_frames": 0,
        "raw_count": 0,
        "images": [],
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
        "elapsed_sec": 0,
    }


def create_task(
    video_path: Path,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
    *,
    task_id: Optional[str] = None,
    source_type: str = "local",
) -> str:
    video_path = Path(video_path)
    task_id = task_id or task_id_from_video_path(video_path)
    _invalidate_task_run(task_id)
    _purge_task_artifacts(task_id, keep_video=video_path)
    out_dir = OUTPUT_DIR / task_id
    run_token = str(uuid.uuid4())

    task = {
        "id": task_id,
        "run_token": run_token,
        "status": "pending",
        "progress": 0,
        "total": 0,
        "message": "queued",
        "video_path": str(video_path),
        "video_name": video_path.name,
        "output_dir": str(out_dir),
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
        "source_type": source_type,
        "result": _empty_result(frame_stride, enable_reid, reid_threshold),
        "error": None,
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
    }

    _register_task(task)

    from services.persistence.monitor_sessions import upsert_analysis_record

    upsert_analysis_record(
        task_id=task_id,
        source_type=source_type if source_type in ("local", "isapi") else "local",
        output_dir=str(out_dir),
        device_name=video_path.name,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )

    thread = threading.Thread(
        target=_run_task,
        args=(
            task_id,
            run_token,
            video_path,
            out_dir,
            frame_stride,
            enable_reid,
            reid_threshold,
            None,
        ),
        daemon=True,
    )
    thread.start()
    return task_id


def create_isapi_tasks(
    host: str,
    username: str,
    password: str,
    channels: List[int],
    start_time: str,
    end_time: str,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
) -> Dict[str, List[str]]:
    stopped_task_ids = stop_active_isapi_analysis()
    unique = sorted(set(int(c) for c in channels))
    if not unique:
        return {"task_ids": [], "stopped_task_ids": stopped_task_ids}

    if len(unique) > 1:
        logger.info(
            "ISAPI: %d channels requested, only analyzing channel %s",
            len(unique),
            unique[0],
        )

    channel = unique[0]
    task_ids: List[str] = []
    task_name = auto_task_name(channel, start_time, end_time)
    task_id = _task_id_from_name(task_name)
    isapi_url = build_isapi_url(host, username, password, channel, start_time, end_time)
    save_path = UPLOAD_DIR / f"{task_id}.mp4"

    _invalidate_task_run(task_id)
    _purge_task_artifacts(task_id)
    out_dir = OUTPUT_DIR / task_id
    run_token = str(uuid.uuid4())

    task = {
        "id": task_id,
        "run_token": run_token,
        "status": "pending",
        "progress": 0,
        "total": 100,
        "message": "queued",
        "video_path": str(save_path),
        "video_name": f"{task_id}.mp4",
        "output_dir": str(out_dir),
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
        "source_type": "isapi",
        "isapi_url": isapi_url,
        "result": _empty_result(frame_stride, enable_reid, reid_threshold),
        "error": None,
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
    }
    _register_task(task)

    from services.persistence.monitor_sessions import upsert_analysis_record

    upsert_analysis_record(
        task_id=task_id,
        source_type="isapi",
        output_dir=str(out_dir),
        device_name=task_name,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )

    thread = threading.Thread(
        target=_run_task,
        args=(
            task_id,
            run_token,
            save_path,
            out_dir,
            frame_stride,
            enable_reid,
            reid_threshold,
            isapi_url,
        ),
        daemon=True,
    )
    thread.start()
    task_ids.append(task_id)
    return {"task_ids": task_ids, "stopped_task_ids": stopped_task_ids}


def _is_current_run(task_id: str, run_token: str) -> bool:
    with _lock:
        t = _tasks.get(task_id)
        return bool(t and t.get("run_token") == run_token)


def _update_task(task_id: str, run_token: str, **fields) -> None:
    if not _is_current_run(task_id, run_token):
        return
    with _lock:
        t = _tasks.get(task_id)
        if t and t.get("run_token") == run_token:
            t.update(fields)


def _plain_progress(task_id: str, run_token: str):
    def on_progress(done, total, msg):
        if not _is_current_run(task_id, run_token):
            return
        with _lock:
            t = _tasks.get(task_id)
            if t and t.get("run_token") == run_token:
                t["progress"] = done
                t["total"] = total
                t["message"] = msg
                t["status"] = "running"

    return on_progress


def _run_task(
    task_id: str,
    run_token: str,
    video_path: Path,
    out_dir: Path,
    frame_stride: int,
    enable_reid: bool,
    reid_threshold: float,
    isapi_url: Optional[str],
):
    from services.persistence.models.monitor_session import STATUS_DOWNLOADING, STATUS_RUNNING
    from services.persistence.monitor_sessions import finish_analysis_session, update_session_status

    def on_analysis_progress(done, total, msg):
        if not _is_current_run(task_id, run_token):
            return
        if total > 0:
            p = int((done / total) * 100)
        else:
            p = 0
        _update_task(
            task_id,
            run_token,
            progress=p,
            total=100,
            message=f"分析中 {p}%",
            status="running",
        )

    def on_download_progress(done, total):
        if not _is_current_run(task_id, run_token):
            return
        if total > 0:
            p = int((done / total) * 100)
        else:
            p = 0
        _update_task(
            task_id,
            run_token,
            progress=p,
            total=100,
            message=f"下载中 {p}%",
            status="downloading",
        )

    def on_result(snapshot: dict) -> None:
        if not _is_current_run(task_id, run_token):
            return
        _update_task(
            task_id,
            run_token,
            status="running",
            result=snapshot,
            message=(
                f"frames={snapshot.get('processed_frames', 0)} "
                f"persons={snapshot.get('saved_count', 0)}"
            ),
        )

    try:
        if not _is_current_run(task_id, run_token):
            return

        video_path = Path(video_path)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        if not isapi_url:
            update_session_status(task_id, STATUS_RUNNING)

        if isapi_url:
            update_session_status(task_id, STATUS_DOWNLOADING)
            _update_task(
                task_id,
                run_token,
                status="downloading",
                message="connecting to NVR",
                progress=0,
            )
            download_video(
                isapi_url,
                str(video_path),
                progress_callback=on_download_progress,
                cancel_check=lambda: not _is_current_run(task_id, run_token),
            )
            if not _is_current_run(task_id, run_token):
                return
            _update_task(
                task_id,
                run_token,
                progress=100,
                total=100,
                message="下载完成",
                status="downloading",
            )
            update_session_status(task_id, STATUS_RUNNING)

        _update_task(task_id, run_token, status="running", message="正在加载模型...", progress=0)
        detector = get_helmet_detector()
        analyzer = OfflineVideoAnalyzer(detector, out_dir, task_id=task_id)
        progress_cb = on_analysis_progress if isapi_url else _plain_progress(task_id, run_token)
        result = analyzer.process(
            str(video_path),
            frame_stride=frame_stride,
            enable_reid=enable_reid,
            reid_threshold=reid_threshold,
            progress_callback=progress_cb,
            result_callback=on_result,
            cancel_check=lambda: not _is_current_run(task_id, run_token),
        )

        if not _is_current_run(task_id, run_token):
            return
        with _lock:
            t = _tasks.get(task_id)
            if not t or t.get("run_token") != run_token:
                return
            t["status"] = "completed"
            t["result"] = result
            if isapi_url:
                t["progress"] = 100
                t["total"] = 100
            else:
                t["progress"] = result.get("processed_frames", t.get("total", 0))
                t["total"] = result.get("sampled_frames", t.get("total", 0))
            t["message"] = "done"
            t["finished_at"] = datetime.now().isoformat()
        finish_analysis_session(task_id, success=True, result=result)
    except Exception as e:
        if not _is_current_run(task_id, run_token):
            return
        with _lock:
            t = _tasks.get(task_id)
            if not t or t.get("run_token") != run_token:
                return
            t["status"] = "failed"
            t["error"] = str(e)
            t["message"] = str(e)
            t["finished_at"] = datetime.now().isoformat()
        finish_analysis_session(task_id, success=False, error_message=str(e))


def _reconcile_rtsp_task(task_id: str) -> None:
    from services.tasks.rtsp_stream import is_rtsp_running

    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("source_type") != "rtsp":
            return
        if t.get("status") not in ("streaming", "pending"):
            return
    if not is_rtsp_running(task_id):
        fail_rtsp_task(task_id, "监控线程已退出，请重新启动")


def get_task(task_id: str) -> Optional[Dict]:
    _reconcile_rtsp_task(task_id)
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return None
        out = dict(t)
        out.pop("isapi_url", None)
        out.pop("rtsp_url", None)
        return out


def list_tasks(source_type: Optional[str] = None) -> List[Dict]:
    with _lock:
        items = []
        for t in sorted(_tasks.values(), key=lambda x: x.get("created_at", ""), reverse=True):
            if source_type and t.get("source_type") != source_type:
                continue
            item = dict(t)
            item.pop("isapi_url", None)
            item.pop("rtsp_url", None)
            items.append(item)
        return items


def _rtsp_on_result(task_id: str, result: dict) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("source_type") != "rtsp":
            return
        if t.get("status") not in ("streaming", "pending"):
            return
        t["status"] = "streaming"
        t["result"] = result
        note = (result.get("status_note") or "").strip()
        frames = int(result.get("processed_frames", 0) or 0)
        persons = int(result.get("saved_count", 0) or 0)
        if frames > 0:
            t["message"] = f"已分析 {frames} 帧 · {persons} 人"
        elif note:
            t["message"] = note
        else:
            t["message"] = "监控中"
        t["progress"] = result.get("processed_frames", 0)


def _rtsp_should_run(task_id: str) -> bool:
    with _lock:
        t = _tasks.get(task_id)
        return bool(t and t.get("status") == "streaming")


def _collect_active_task_ids_for_device(device_id: int) -> List[str]:
    from services.persistence.monitor_sessions import list_streaming_task_ids_for_device
    from services.tasks.rtsp_stream import is_rtsp_running, list_running_task_ids

    ids: set = set()
    with _lock:
        for t in _tasks.values():
            if t.get("source_type") != "rtsp":
                continue
            if t.get("device_id") != device_id:
                continue
            if t.get("status") in ("streaming", "pending", "stopping"):
                ids.add(t["id"])
    for tid in list_running_task_ids():
        if not is_rtsp_running(tid):
            continue
        t = _tasks.get(tid)
        if t and t.get("device_id") == device_id:
            ids.add(tid)
    for tid in list_streaming_task_ids_for_device(device_id):
        ids.add(tid)
    return list(ids)


def stop_rtsp_for_device(device_id: int) -> List[str]:
    """Stop all active RTSP tasks for one device (memory + DB)."""
    from services.persistence.models.monitor_session import STATUS_STOPPED
    from services.persistence.monitor_sessions import update_session_status
    from services.tasks.rtsp_stream import is_rtsp_running

    task_ids = _collect_active_task_ids_for_device(device_id)
    for tid in task_ids:
        stop_rtsp_task(tid)
    for tid in task_ids:
        try:
            _stop_rtsp_and_wait(tid, timeout_sec=10.0)
        except RuntimeError:
            pass
        if not is_rtsp_running(tid):
            update_session_status(tid, STATUS_STOPPED)
    return task_ids


def list_active_rtsp_tasks() -> List[Dict]:
    from services.tasks.rtsp_stream import is_rtsp_running, list_running_task_ids

    items: List[Dict] = []
    seen: set = set()
    for tid in list_running_task_ids():
        if tid in seen or not is_rtsp_running(tid):
            continue
        t = get_task(tid)
        if not t or t.get("source_type") != "rtsp":
            continue
        if t.get("status") not in ("streaming", "pending"):
            continue
        seen.add(tid)
        items.append(
            {
                "task_id": tid,
                "device_id": t.get("device_id"),
                "device_name": t.get("video_name") or tid,
            }
        )
    items.sort(key=lambda x: x.get("device_id") or 0)
    return items


def create_rtsp_task(
    rtsp_url: str,
    task_name: Optional[str] = None,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
    device_id: Optional[int] = None,
    device_name: Optional[str] = None,
) -> str:
    from services.persistence.monitor_sessions import record_session_start
    from services.tasks.rtsp_stream import start_rtsp_stream

    if device_id is not None:
        stop_rtsp_for_device(int(device_id))

    base = task_name or f"RTSP_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task_id = _rtsp_task_id(base)
    out_dir = OUTPUT_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    display_name = (device_name or task_name or base).strip() or task_id

    task = {
        "id": task_id,
        "run_token": str(uuid.uuid4()),
        "status": "streaming",
        "progress": 0,
        "total": 0,
        "message": "connecting",
        "video_path": "",
        "video_name": display_name,
        "output_dir": str(out_dir),
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
        "source_type": "rtsp",
        "rtsp_url": rtsp_url.strip(),
        "device_id": device_id,
        "result": _empty_result(frame_stride, enable_reid, reid_threshold),
        "error": None,
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
    }
    _register_task(task)

    record_session_start(
        task_id=task_id,
        output_dir=str(out_dir),
        device_id=device_id,
        device_name=display_name,
        source_type="rtsp",
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )

    start_rtsp_stream(
        task_id,
        rtsp_url,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )
    return task_id


def create_rtsp_tasks_from_devices(
    devices: List[Dict],
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
) -> List[str]:
    """Start one RTSP task per device dict {id, name, rtsp_url}."""
    task_ids: List[str] = []
    for dev in devices:
        dev_id = int(dev["id"])
        name = (dev.get("name") or f"设备{dev_id}").strip()
        rtsp_url = (dev.get("rtsp_url") or "").strip()
        if not rtsp_url.lower().startswith("rtsp://"):
            raise ValueError(f"设备 {name} 的 RTSP 地址无效")
        task_name = f"{name}_{dev_id}"
        task_ids.append(
            create_rtsp_task(
                rtsp_url,
                task_name=task_name,
                frame_stride=frame_stride,
                enable_reid=enable_reid,
                reid_threshold=reid_threshold,
                device_id=dev_id,
                device_name=name,
            )
        )
    return task_ids


def stop_rtsp_task(task_id: str) -> bool:
    from services.tasks.rtsp_stream import is_rtsp_running, request_stop

    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("source_type") != "rtsp":
            return False
        if t.get("status") not in ("streaming", "pending"):
            return False
        t["status"] = "stopped"
        t["message"] = "stopping"
    request_stop(task_id)
    return True


def finish_rtsp_task(task_id: str, stopped: bool = False) -> None:
    from services.persistence.models.monitor_session import STATUS_COMPLETED, STATUS_STOPPED
    from services.persistence.monitor_sessions import update_session_status
    from services.persistence.result_store import save_session_result

    result = None
    output_dir = None
    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("source_type") != "rtsp":
            return
        result = t.get("result")
        output_dir = t.get("output_dir")
        t["status"] = "stopped" if stopped else "completed"
        t["message"] = "stopped" if stopped else "done"
        t["finished_at"] = datetime.now().isoformat()

    if output_dir and result:
        save_session_result(output_dir, result)

    update_session_status(
        task_id,
        STATUS_STOPPED if stopped else STATUS_COMPLETED,
    )


def fail_rtsp_task(task_id: str, error: str) -> None:
    from services.persistence.models.monitor_session import STATUS_FAILED
    from services.persistence.monitor_sessions import update_session_status
    from services.persistence.result_store import save_session_result

    result = None
    output_dir = None
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        result = t.get("result")
        output_dir = t.get("output_dir")
        t["status"] = "failed"
        t["error"] = error
        t["message"] = error
        t["finished_at"] = datetime.now().isoformat()

    if output_dir and result:
        save_session_result(output_dir, result)

    update_session_status(task_id, STATUS_FAILED, error_message=error)


def _init_rtsp_callbacks() -> None:
    from services.tasks.rtsp_stream import configure_callbacks

    configure_callbacks(on_result=_rtsp_on_result, should_run=_rtsp_should_run)


_init_rtsp_callbacks()


def ensure_dirs():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
