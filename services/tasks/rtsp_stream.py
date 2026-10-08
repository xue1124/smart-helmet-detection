# -*- coding: utf-8 -*-
"""RTSP live stream: read frames, detect no-helmet, incremental OSNet merge."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional

import cv2

from services.analysis.helmet_detector import crop_box_for_violation
from services.analysis.violation_store import LiveResultStore
from services.integrations.sms_client import is_sms_enabled
from services.config import OUTPUT_DIR

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

FFMPEG_PATH = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"

PREVIEW_MAX_WIDTH = int(os.environ.get("PREVIEW_MAX_WIDTH", "960"))
PREVIEW_JPEG_QUALITY = int(os.environ.get("PREVIEW_JPEG_QUALITY", "75"))
PREVIEW_MIN_INTERVAL = float(os.environ.get("PREVIEW_MIN_INTERVAL", "0.08"))
READ_FAIL_BEFORE_RECONNECT = int(os.environ.get("RTSP_READ_FAIL_BEFORE_RECONNECT", "150"))
RECONNECT_DELAY_SEC = float(os.environ.get("RTSP_RECONNECT_DELAY_SEC", "3"))
RECONNECT_OPEN_TIMEOUT_SEC = float(os.environ.get("RTSP_RECONNECT_OPEN_TIMEOUT_SEC", "15"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def rtsp_save_archive() -> bool:
    return _env_bool("RTSP_SAVE_ARCHIVE", False)

_lock = threading.Lock()
_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}
_caps: Dict[str, cv2.VideoCapture] = {}
_caps_lock = threading.Lock()
_recorders: Dict[str, subprocess.Popen] = {}
_recorders_lock = threading.Lock()
_preview_jpeg: Dict[str, bytes] = {}
_preview_version: Dict[str, int] = {}
_preview_lock = threading.Lock()

_on_result: Optional[Callable[[str, dict], None]] = None
_should_run: Optional[Callable[[str], bool]] = None


def configure_callbacks(
    on_result: Callable[[str, dict], None],
    should_run: Callable[[str], bool],
) -> None:
    global _on_result, _should_run
    _on_result = on_result
    _should_run = should_run


def is_rtsp_running(task_id: str) -> bool:
    with _lock:
        t = _threads.get(task_id)
        return t is not None and t.is_alive()


def list_running_task_ids() -> List[str]:
    with _lock:
        return [tid for tid, t in _threads.items() if t is not None and t.is_alive()]


def _archive_path(task_id: str) -> Path:
    return OUTPUT_DIR / task_id / "live_archive.mp4"


def _open_capture(rtsp_url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    for prop, val in (
        (getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None), 10000),
        (getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None), 5000),
    ):
        if prop is None:
            continue
        try:
            cap.set(prop, val)
        except Exception:
            pass
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def _open_capture_with_timeout(rtsp_url: str, timeout_sec: float = 15.0) -> cv2.VideoCapture:
    box: Dict[str, object] = {"cap": None, "err": None}

    def _worker() -> None:
        try:
            box["cap"] = _open_capture(rtsp_url)
        except Exception as exc:
            box["err"] = exc

    opener = threading.Thread(target=_worker, name="rtsp-open", daemon=True)
    opener.start()
    opener.join(timeout=timeout_sec)
    if opener.is_alive():
        raise RuntimeError(f"连接 RTSP 超时（{int(timeout_sec)}s）: {rtsp_url}")
    if box["err"] is not None:
        raise RuntimeError(str(box["err"]))
    cap = box["cap"]
    if cap is None or not cap.isOpened():
        raise RuntimeError(f"无法打开 RTSP 流: {rtsp_url}")
    return cap


def _start_ffmpeg_recorder(task_id: str, rtsp_url: str) -> subprocess.Popen:
    out_path = _archive_path(task_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass
    cmd = [
        FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-y",
        str(out_path),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        err = ""
        try:
            err = (proc.stderr.read() or b"").decode("utf-8", errors="ignore")[:500]
        except Exception:
            pass
        raise RuntimeError(f"ffmpeg 录制启动失败: {err or '进程已退出'}")
    with _recorders_lock:
        _recorders[task_id] = proc
    return proc


def _stop_ffmpeg_recorder(task_id: str) -> None:
    with _recorders_lock:
        proc = _recorders.pop(task_id, None)
    if proc is None:
        return
    if proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _release_cap(task_id: str) -> None:
    with _caps_lock:
        cap = _caps.pop(task_id, None)
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


def request_stop(task_id: str) -> None:
    with _lock:
        ev = _stop_events.get(task_id)
        if ev:
            ev.set()
    _stop_ffmpeg_recorder(task_id)


def _crop_region(im0, box):
    h, w = im0.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return im0
    return im0[y1:y2, x1:x2]


def _resize_for_preview(frame):
    h, w = frame.shape[:2]
    if w <= PREVIEW_MAX_WIDTH:
        return frame
    scale = PREVIEW_MAX_WIDTH / w
    return cv2.resize(frame, (PREVIEW_MAX_WIDTH, int(h * scale)))


def _set_preview_jpeg(task_id: str, frame_bgr) -> None:
    resized = _resize_for_preview(frame_bgr)
    ok, buf = cv2.imencode(
        ".jpg",
        resized,
        [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_JPEG_QUALITY],
    )
    if not ok:
        return
    with _preview_lock:
        _preview_jpeg[task_id] = buf.tobytes()
        _preview_version[task_id] = _preview_version.get(task_id, 0) + 1


def _clear_preview(task_id: str) -> None:
    with _preview_lock:
        _preview_jpeg.pop(task_id, None)
        _preview_version.pop(task_id, None)


def iter_preview_mjpeg(task_id: str) -> Generator[bytes, None, None]:
    """Yield multipart MJPEG chunks for browser preview."""
    boundary = b"frame"
    last_ver = -1
    idle_ticks = 0
    while idle_ticks < 50:
        with _preview_lock:
            jpeg = _preview_jpeg.get(task_id)
            ver = _preview_version.get(task_id, 0)
        running = is_rtsp_running(task_id)

        if jpeg and ver != last_ver:
            last_ver = ver
            idle_ticks = 0
            yield (
                b"--"
                + boundary
                + b"\r\nContent-Type: image/jpeg\r\n\r\n"
                + jpeg
                + b"\r\n"
            )
        else:
            idle_ticks += 1
            time.sleep(PREVIEW_MIN_INTERVAL if running else 0.2)

        if running:
            idle_ticks = min(idle_ticks, 10)

        if not running and not jpeg:
            break


def _run_stream(
    task_id: str,
    rtsp_url: str,
    frame_stride: int,
    enable_reid: bool,
    reid_threshold: float,
    stop_event: threading.Event,
) -> None:
    cap: Optional[cv2.VideoCapture] = None
    out_dir = OUTPUT_DIR / task_id
    from services.tasks.task_manager import get_task

    task = get_task(task_id) or {}
    device_name = (task.get("video_name") or "").strip()
    store = LiveResultStore(
        task_id,
        out_dir,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
        sms_notify=is_sms_enabled(),
        device_name=device_name,
    )
    store.frame_stride = max(1, int(frame_stride))
    t0 = time.time()
    status_note = "正在连接视频流..."

    def push_result(note: Optional[str] = None) -> None:
        nonlocal status_note
        if note:
            status_note = note
        if _on_result is None:
            return
        snap = store.snapshot()
        snap["elapsed_sec"] = round(time.time() - t0, 2)
        if status_note and status_note not in ("", "监控中"):
            snap["status_note"] = status_note
        elif snap.get("processed_frames", 0) > 0:
            snap["status_note"] = ""
        else:
            snap["status_note"] = status_note
        _on_result(task_id, snap)

    def alive() -> bool:
        if stop_event.is_set():
            return False
        if _should_run and not _should_run(task_id):
            return False
        return True

    heartbeat_stop = threading.Event()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(2.0):
            if stop_event.is_set():
                break
            push_result()

    hb = threading.Thread(target=_heartbeat, name=f"rtsp-hb-{task_id[:16]}", daemon=True)
    hb.start()

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        push_result("正在连接视频流...")

        if rtsp_save_archive():
            try:
                _start_ffmpeg_recorder(task_id, rtsp_url)
            except Exception as e:
                print(f"[RTSP] 录像未启动（继续分析）: {e}")

        if not alive():
            return

        from services.tasks.task_manager import get_helmet_detector

        push_result("正在加载模型...")
        detector = get_helmet_detector()
        push_result("正在打开 RTSP 流...")
        cap = _open_capture_with_timeout(rtsp_url)
        with _caps_lock:
            _caps[task_id] = cap

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if fps <= 0:
            fps = 25.0

        frame_idx = 0
        fail_streak = 0
        skip = store.frame_stride
        last_det = None
        last_violation_heads: List[List[float]] = []
        last_preview_ts = 0.0
        push_result("监控中")

        def release_local_cap() -> None:
            nonlocal cap
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
            with _caps_lock:
                _caps.pop(task_id, None)

        def restart_archive_if_needed() -> None:
            if not rtsp_save_archive():
                return
            _stop_ffmpeg_recorder(task_id)
            try:
                _start_ffmpeg_recorder(task_id, rtsp_url)
            except Exception as e:
                print(f"[RTSP] 重连后录像未启动（继续分析）: {e}")

        def reconnect_stream() -> bool:
            nonlocal cap, fps, fail_streak
            release_local_cap()
            restart_archive_if_needed()
            push_result("连接中断，正在重连...")
            while alive():
                time.sleep(RECONNECT_DELAY_SEC)
                if not alive():
                    return False
                try:
                    new_cap = _open_capture_with_timeout(rtsp_url, RECONNECT_OPEN_TIMEOUT_SEC)
                except Exception as e:
                    print(f"[RTSP] 重连失败 | {task_id} | {e}")
                    push_result("连接中断，正在重连...")
                    continue
                cap = new_cap
                with _caps_lock:
                    _caps[task_id] = cap
                new_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                if new_fps > 0:
                    fps = new_fps
                fail_streak = 0
                push_result("监控中")
                return True
            return False

        while alive():
            ok, frame = cap.read()
            if not ok or frame is None:
                fail_streak += 1
                if fail_streak > READ_FAIL_BEFORE_RECONNECT:
                    if not reconnect_stream():
                        break
                    continue
                if not alive():
                    break
                time.sleep(0.02)
                continue
            fail_streak = 0

            if not alive():
                break

            is_sample = frame_idx % skip == 0
            if is_sample:
                try:
                    det = detector.predict(frame)
                    violations = detector.find_no_helmet_heads(det)
                    last_det = det
                    last_violation_heads = [h for h, _ in violations]
                    had_violation = bool(violations)
                    store.tick_frame(violation=had_violation)

                    if violations:
                        for head_box, head_conf in violations:
                            person_box = detector.find_person_for_head(det, head_box)
                            crop_box = crop_box_for_violation(head_box, person_box, frame.shape)
                            crop = _crop_region(frame, crop_box)
                            store.add_detection(crop, frame_idx, fps, head_conf)

                    push_result()
                except Exception as e:
                    print(f"[RTSP] 单帧推理跳过 | {task_id} | frame={frame_idx} | {e}")
                    store.tick_frame(violation=False)
                    push_result()

            now = time.time()
            if now - last_preview_ts >= PREVIEW_MIN_INTERVAL:
                try:
                    if last_det is not None:
                        display = detector.draw_detections(frame, last_det, last_violation_heads)
                    else:
                        display = frame
                except Exception as e:
                    print(f"[RTSP] 预览绘制跳过 | {task_id} | frame={frame_idx} | {e}")
                    display = frame
                _set_preview_jpeg(task_id, display)
                last_preview_ts = now

            frame_idx += 1

        push_result("已停止" if stop_event.is_set() else "监控结束")
    except Exception as e:
        if not stop_event.is_set():
            raise
        print(f"[RTSP] 停止 | {task_id} | {e}")
    finally:
        heartbeat_stop.set()
        _stop_ffmpeg_recorder(task_id)
        _release_cap(task_id)
        _clear_preview(task_id)
        with _lock:
            _threads.pop(task_id, None)
            _stop_events.pop(task_id, None)


def start_rtsp_stream(
    task_id: str,
    rtsp_url: str,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
) -> None:
    url = (rtsp_url or "").strip()
    if not url.lower().startswith("rtsp://"):
        raise ValueError("RTSP 地址必须以 rtsp:// 开头")

    with _lock:
        old = _threads.get(task_id)
        if old is not None and old.is_alive():
            request_stop(task_id)
            old.join(timeout=10.0)
            if old.is_alive():
                raise RuntimeError("该 RTSP 任务仍在停止中，请稍后再试")

    stop_event = threading.Event()
    t = threading.Thread(
        target=_run_stream_wrapper,
        args=(task_id, url, frame_stride, enable_reid, reid_threshold, stop_event),
        name=f"rtsp-{task_id[:24]}",
        daemon=True,
    )
    with _lock:
        _stop_events[task_id] = stop_event
        _threads[task_id] = t
    t.start()


def _run_stream_wrapper(
    task_id: str,
    rtsp_url: str,
    frame_stride: int,
    enable_reid: bool,
    reid_threshold: float,
    stop_event: threading.Event,
) -> None:
    from services.tasks import task_manager as tm

    try:
        _run_stream(task_id, rtsp_url, frame_stride, enable_reid, reid_threshold, stop_event)
    except Exception as e:
        if stop_event.is_set():
            tm.finish_rtsp_task(task_id, stopped=True)
        else:
            tm.fail_rtsp_task(task_id, str(e))
        return
    tm.finish_rtsp_task(task_id, stopped=stop_event.is_set())
