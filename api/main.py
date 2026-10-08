# -*- coding: utf-8 -*-
"""FastAPI backend for safety helmet video analysis."""

import shutil
import uuid
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv

from services.config import FRONTEND_DIST, PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.persistence.database import check_db_connection, get_db
from services.persistence.device_service import (
    create_device,
    delete_device,
    device_to_dict,
    get_device,
    list_devices,
    probe_device,
    update_device,
)
from services.persistence.monitor_sessions import (
    SessionDeleteError,
    delete_monitor_session,
    get_session_detail,
    list_sessions,
    reconcile_orphan_streaming_sessions,
)
from services.integrations.isapi_client import check_ffmpeg, discover_channels
from services.tasks.task_manager import (
    OUTPUT_DIR,
    UPLOAD_DIR,
    create_isapi_tasks,
    create_rtsp_task,
    create_rtsp_tasks_from_devices,
    create_task,
    ensure_dirs,
    get_helmet_detector,
    get_task,
    list_active_rtsp_tasks,
    list_tasks,
    stop_rtsp_task,
    stop_video_analysis,
)
from services.tasks.rtsp_stream import is_rtsp_running, iter_preview_mjpeg
from services.analysis.person_reid import get_reid_extractor
from services.auth.auth_service import (
    TOKEN_COOKIE,
    TOKEN_TTL_HOURS,
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_user_by_id,
    is_public_api_path,
    user_to_dict,
)

API_PORT = 18765
SERVER_BOOT_ID = str(uuid.uuid4())

app = FastAPI(
    title="Smart Construction - Helmet Detection API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ensure_dirs()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/v1/") and not is_public_api_path(path):
        token = request.cookies.get(TOKEN_COOKIE)
        if not token:
            auth_header = request.headers.get("Authorization") or ""
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
        payload = decode_access_token(token) if token else None
        if not payload:
            return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
        request.state.user_id = payload.get("uid")
        request.state.username = payload.get("sub")
        request.state.role = payload.get("role")
    return await call_next(request)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class DeviceCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    rtsp_url: str = Field(..., min_length=7, max_length=512)
    location: Optional[str] = Field(None, max_length=200)
    enabled: bool = True
    remark: Optional[str] = Field(None, max_length=500)


class DeviceUpdateBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    rtsp_url: Optional[str] = Field(None, min_length=7, max_length=512)
    location: Optional[str] = Field(None, max_length=200)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(None, max_length=500)


def _device_label(device_type: str) -> str:
    return "GPU" if device_type == "cuda" else "CPU"


@app.on_event("startup")
def warmup_model():
    print("=" * 52)
    print("Smart Construction API 模型预热")
    try:
        detector = get_helmet_detector()
        print(f"[YOLO] 使用设备: {_device_label(detector.device.type)} ({detector.device})")
    except Exception as e:
        print(f"[warn] YOLO 加载失败: {e}")
    try:
        reid = get_reid_extractor()
        provider = reid.session.get_providers()[0]
        reid_dev = "GPU" if "CUDA" in provider else "CPU"
        print(f"[OSNet ReID] 使用设备: {reid_dev} ({provider})")
        print(f"[OSNet ReID] 模型: {reid.model_path}")
    except Exception as e:
        print(f"[warn] OSNet 加载失败: {e}")
    ok, msg = check_db_connection()
    if ok:
        print("[MySQL] 连接正常")
        n = reconcile_orphan_streaming_sessions()
        if n:
            print(f"[MySQL] 已清理 {n} 条孤儿 streaming 会话")
    else:
        print(f"[warn] MySQL 连接失败: {msg}")
    print(f"[服务] http://127.0.0.1:{API_PORT}")
    print("=" * 52)


@app.get("/api/v1/health")
def health():
    try:
        detector = get_helmet_detector()
        reid = get_reid_extractor()
        reid_provider = reid.session.get_providers()[0]
        db_ok, db_msg = check_db_connection()
        return {
            "status": "ok",
            "boot_id": SERVER_BOOT_ID,
            "weights": detector.weights,
            "classes": list(detector.names),
            "devices": {
                "yolo": _device_label(detector.device.type),
                "reid": "GPU" if "CUDA" in reid_provider else "CPU",
            },
            "ffmpeg": check_ffmpeg(),
            "database": {"ok": db_ok, "detail": db_msg},
        }
    except Exception as e:
        return {"status": "error", "boot_id": SERVER_BOOT_ID, "detail": str(e)}


@app.post("/api/v1/auth/login")
def auth_login(body: LoginBody, db: Session = Depends(get_db)):
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    token = create_access_token(user.id, user.username, user.role)
    resp = JSONResponse({"user": user_to_dict(user)})
    resp.set_cookie(
        TOKEN_COOKIE,
        token,
        httponly=True,
        max_age=TOKEN_TTL_HOURS * 3600,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/v1/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(TOKEN_COOKIE, path="/")
    return resp


@app.get("/api/v1/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "未登录")
    user = get_user_by_id(db, int(user_id))
    if not user or not user.enabled:
        raise HTTPException(401, "用户不存在或已禁用")
    return {"user": user_to_dict(user)}


@app.post("/api/v1/tasks/video")
async def create_video_task(
    file: UploadFile = File(...),
    frame_stride: int = Form(25),
    enable_reid: bool = Form(True),
    reid_threshold: float = Form(0.7),
):
    if not file.filename:
        raise HTTPException(400, "empty filename")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".m4v", ".mpeg", ".mpg"}:
        raise HTTPException(400, f"unsupported video format: {suffix}")

    frame_stride = max(1, min(frame_stride, 120))
    reid_threshold = max(0.5, min(float(reid_threshold), 0.99))

    save_path = UPLOAD_DIR / file.filename
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    task_id = create_task(
        save_path,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
        source_type="local",
    )
    return {
        "task_id": task_id,
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
    }


@app.post("/api/v1/isapi/channels")
def detect_isapi_channels(
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    if not host.strip() or not username.strip() or not str(password).strip():
        raise HTTPException(400, "请先填写设备 IP、用户名和密码")
    try:
        channels = discover_channels(host.strip(), username.strip(), password)
    except Exception as e:
        raise HTTPException(502, str(e)) from e
    return {"channels": channels}


@app.post("/api/v1/tasks/isapi")
def create_isapi_video_tasks(
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    isapi_start: str = Form(...),
    isapi_end: str = Form(...),
    channels: List[int] = Form(...),
    frame_stride: int = Form(25),
    enable_reid: bool = Form(True),
    reid_threshold: float = Form(0.7),
):
    if not check_ffmpeg():
        raise HTTPException(
            500,
            "未检测到 ffmpeg，ISAPI 录像合并需要 ffmpeg。请安装并加入 PATH，或设置 FFMPEG_PATH。",
        )
    if not host.strip() or not username.strip() or not str(password).strip():
        raise HTTPException(400, "ISAPI 参数不完整（需填写设备 IP、用户名、密码）")
    if not isapi_start or not isapi_end:
        raise HTTPException(400, "请填写开始/结束时间")
    if not channels:
        raise HTTPException(400, "请至少选择一个通道")
    try:
        start_dt = datetime.fromisoformat(isapi_start)
        end_dt = datetime.fromisoformat(isapi_end)
    except ValueError as e:
        raise HTTPException(400, "开始/结束时间格式无效，请使用 ISO 格式") from e
    if end_dt <= start_dt:
        raise HTTPException(400, "结束时间必须晚于开始时间")

    frame_stride = max(1, min(frame_stride, 120))
    reid_threshold = max(0.5, min(float(reid_threshold), 0.99))

    task_ids = create_isapi_tasks(
        host.strip(),
        username.strip(),
        password,
        channels,
        isapi_start,
        isapi_end,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )
    return {
        "task_ids": task_ids["task_ids"],
        "stopped_task_ids": task_ids["stopped_task_ids"],
        "count": len(task_ids["task_ids"]),
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
    }


@app.get("/api/v1/tasks")
def list_all_tasks(source_type: Optional[str] = None):
    return {"tasks": list_tasks(source_type=source_type)}


@app.get("/api/v1/devices")
def api_list_devices(
    db: Session = Depends(get_db),
    name: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    status: Optional[int] = Query(None, ge=0, le=2),
    enabled_only: bool = Query(False),
):
    items = list_devices(
        db,
        name=name,
        location=location,
        status=status,
        enabled_only=enabled_only,
    )
    return {"devices": [device_to_dict(d) for d in items], "count": len(items)}


@app.post("/api/v1/devices")
def api_create_device(body: DeviceCreateBody, db: Session = Depends(get_db)):
    try:
        device = create_device(
            db,
            name=body.name,
            rtsp_url=body.rtsp_url,
            location=body.location,
            enabled=body.enabled,
            remark=body.remark,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return device_to_dict(device)


@app.get("/api/v1/devices/{device_id}")
def api_get_device(device_id: int, db: Session = Depends(get_db)):
    device = get_device(db, device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    return device_to_dict(device)


@app.put("/api/v1/devices/{device_id}")
def api_update_device(device_id: int, body: DeviceUpdateBody, db: Session = Depends(get_db)):
    fields = body.model_fields_set
    kwargs = {}
    if "name" in fields:
        kwargs["name"] = body.name
    if "rtsp_url" in fields:
        kwargs["rtsp_url"] = body.rtsp_url
    if "location" in fields:
        kwargs["location"] = body.location
    if "enabled" in fields:
        kwargs["enabled"] = body.enabled
    if "remark" in fields:
        kwargs["remark"] = body.remark
    try:
        device = update_device(db, device_id, **kwargs)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return device_to_dict(device)


@app.delete("/api/v1/devices/{device_id}")
def api_delete_device(device_id: int, db: Session = Depends(get_db)):
    try:
        delete_device(db, device_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True, "id": device_id}


@app.post("/api/v1/devices/{device_id}/probe")
def api_probe_device(device_id: int, db: Session = Depends(get_db)):
    try:
        device = probe_device(db, device_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return device_to_dict(device)


def _parse_query_datetime(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            dt = datetime.fromisoformat(raw)
            if end_of_day:
                return dt.replace(hour=23, minute=59, second=59)
            return dt
        return datetime.fromisoformat(raw)
    except ValueError as e:
        raise HTTPException(400, f"时间格式无效: {value}") from e


@app.get("/api/v1/monitor-sessions")
def api_list_monitor_sessions(
    db: Session = Depends(get_db),
    device_id: Optional[int] = Query(None),
    source_type: Optional[str] = Query(None, description="rtsp/local/isapi"),
    status: Optional[str] = Query(None),
    started_from: Optional[str] = Query(None, description="ISO 日期或 datetime"),
    started_to: Optional[str] = Query(None, description="ISO 日期或 datetime"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    items, total = list_sessions(
        db,
        device_id=device_id,
        source_type=(source_type or "").strip() or None,
        status=(status or "").strip() or None,
        started_from=_parse_query_datetime(started_from),
        started_to=_parse_query_datetime(started_to, end_of_day=True),
        page=page,
        page_size=page_size,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/v1/monitor-sessions/{session_id}")
def api_get_monitor_session(session_id: int, db: Session = Depends(get_db)):
    detail = get_session_detail(db, session_id)
    if not detail:
        raise HTTPException(404, "记录不存在")
    return detail


@app.delete("/api/v1/monitor-sessions/{session_id}")
def api_delete_monitor_session(session_id: int, db: Session = Depends(get_db)):
    try:
        delete_monitor_session(db, session_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except SessionDeleteError as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "id": session_id}


@app.post("/api/v1/stream/live/start-by-device")
def start_rtsp_by_device(
    device_ids: List[int] = Form(...),
    frame_stride: int = Form(25),
    enable_reid: bool = Form(True),
    reid_threshold: float = Form(0.7),
    db: Session = Depends(get_db),
):
    if not device_ids:
        raise HTTPException(400, "请至少选择一个设备")
    frame_stride = max(1, min(frame_stride, 120))
    reid_threshold = max(0.5, min(float(reid_threshold), 0.99))

    selected = []
    for did in sorted(set(int(i) for i in device_ids)):
        device = get_device(db, did)
        if not device:
            raise HTTPException(404, f"设备不存在: {did}")
        if not device.enabled:
            raise HTTPException(400, f"设备已禁用: {device.name}")
        selected.append(
            {"id": device.id, "name": device.name, "rtsp_url": device.rtsp_url}
        )

    try:
        task_ids = create_rtsp_tasks_from_devices(
            selected,
            frame_stride=frame_stride,
            enable_reid=enable_reid,
            reid_threshold=reid_threshold,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

    return {
        "task_ids": task_ids,
        "count": len(task_ids),
        "device_ids": [d["id"] for d in selected],
    }


@app.post("/api/v1/stream/live/start")
def start_rtsp_live(
    rtsp_url: str = Form(...),
    task_name: str = Form(...),
    frame_stride: int = Form(25),
    enable_reid: bool = Form(True),
    reid_threshold: float = Form(0.7),
):
    url = (rtsp_url or "").strip()
    if not url.lower().startswith("rtsp://"):
        raise HTTPException(400, "RTSP 地址必须以 rtsp:// 开头")
    name = (task_name or "").strip()
    if not name:
        raise HTTPException(400, "请填写任务名称")
    if frame_stride < 1:
        raise HTTPException(400, "抽帧步长至少为 1")

    frame_stride = max(1, min(frame_stride, 120))
    reid_threshold = max(0.5, min(float(reid_threshold), 0.99))
    try:
        task_id = create_rtsp_task(
            url,
            task_name=name,
            frame_stride=frame_stride,
            enable_reid=enable_reid,
            reid_threshold=reid_threshold,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {"task_id": task_id, "status": "streaming", "rtsp_url": url}


@app.get("/api/v1/stream/live/active")
def list_active_rtsp_live():
    sessions = list_active_rtsp_tasks()
    return {"sessions": sessions, "count": len(sessions)}


@app.post("/api/v1/stream/live/stop")
def stop_rtsp_live(task_id: str = Form(...)):
    from services.persistence.monitor_sessions import stop_stale_streaming_session
    from services.tasks.rtsp_stream import request_stop

    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(400, "task_id 不能为空")
    task = get_task(tid)
    if task and task.get("source_type") != "rtsp":
        raise HTTPException(400, "仅 RTSP 实时任务可停止")
    if task and stop_rtsp_task(tid):
        return {"task_id": tid, "status": "stopping"}
    if is_rtsp_running(tid):
        request_stop(tid)
        return {"task_id": tid, "status": "stopping"}
    if stop_stale_streaming_session(tid, reason="用户停止"):
        return {"task_id": tid, "status": "stopped"}
    if task and task.get("status") in ("streaming", "pending"):
        return {"task_id": tid, "status": "stopped"}
    raise HTTPException(404, "任务不存在或无法停止")


@app.get("/api/v1/stream/live/preview/{task_id}")
def preview_rtsp_live(task_id: str):
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(400, "task_id 不能为空")
    task = get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    if task.get("source_type") != "rtsp":
        raise HTTPException(400, "仅 RTSP 实时任务支持预览")
    if task.get("status") not in ("streaming", "pending") and not is_rtsp_running(tid):
        raise HTTPException(400, "任务未在直播中")
    return StreamingResponse(
        iter_preview_mjpeg(tid),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@app.post("/api/v1/tasks/stop")
def stop_video_analysis_task(task_id: str = Form(...)):
    from services.persistence.monitor_sessions import stop_stale_analysis_session

    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(400, "task_id 不能为空")
    task = get_task(tid)
    if task and task.get("source_type") not in ("isapi", "local"):
        raise HTTPException(400, "仅本地上传或录像回放任务可停止")
    if stop_video_analysis(tid, reason="用户停止"):
        updated = get_task(tid)
        return {
            "task_id": tid,
            "status": (updated or {}).get("status") or "stopped",
        }
    if stop_stale_analysis_session(tid, reason="用户停止"):
        return {"task_id": tid, "status": "stopped"}
    if task and task.get("status") not in ("pending", "downloading", "running"):
        raise HTTPException(400, "任务未在分析中")
    raise HTTPException(404, "任务不存在或无法停止")


@app.get("/api/v1/tasks/{task_id}")
def task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task


@app.get("/api/v1/files/crops/{file_path:path}")
def get_crop_file(file_path: str, task_id: Optional[str] = None):
    if task_id:
        path = OUTPUT_DIR / task_id / "crops" / file_path
    else:
        # search latest matching file across tasks
        path = None
        for d in sorted(OUTPUT_DIR.glob("*/crops"), reverse=True):
            p = d / file_path
            if p.exists():
                path = p
                break
        if path is None:
            raise HTTPException(404, "file not found")
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path)


@app.get("/api/v1/tasks/{task_id}/files/{subdir}/{file_path:path}")
def get_task_file(task_id: str, subdir: str, file_path: str):
    if subdir not in ("crops", "frames"):
        raise HTTPException(400, "invalid subdir")
    path = OUTPUT_DIR / task_id / subdir / file_path
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path)


# Serve Vite build (only / and /assets/*, does not shadow /api/*)
if FRONTEND_DIST.exists():

    @app.get("/")
    async def spa_index():
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/assets/{asset_path:path}")
    async def spa_assets(asset_path: str):
        target = FRONTEND_DIST / "assets" / asset_path
        if target.is_file():
            return FileResponse(target)
        raise HTTPException(404, "asset not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=API_PORT, reload=False)
