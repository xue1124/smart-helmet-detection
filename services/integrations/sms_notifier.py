# -*- coding: utf-8 -*-
"""异步调度短信：拼模板、读号码、防重复，再调用 sms_client。"""

from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from services.integrations.sms_client import is_sms_enabled, send_sms

logger = logging.getLogger(__name__)

_SMS_TEMPLATE = (
    "安全预警：{time}在{device_name}摄像头下发生{count}起未戴安全帽事件，请及时处理！"
)

_queue: queue.Queue = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False
_cooldown: Dict[Tuple[str, int], datetime] = {}


def _parse_phones() -> list[str]:
    raw = os.environ.get("SMS_PHONES", "")
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def _cooldown_minutes() -> float:
    raw = (os.environ.get("SMS_COOLDOWN_MINUTES") or "30").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


def _build_content(time_str: str, count: int, device_name: str = "") -> str:
    # 互亿报备模板要求时间格式如 2026年06月13日16:07（非 ISO 格式）
    event_time = datetime.now().strftime("%Y年%m月%d日%H:%M")
    name = (device_name or "").strip() or "未知"
    if name.endswith("摄像头"):
        name = name[:-3].strip() or "未知"
    return _SMS_TEMPLATE.format(time=event_time, device_name=name, count=max(1, int(count)))


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="sms-notifier", daemon=True)
        t.start()
        _worker_started = True


def _in_cooldown(task_id: str, person_id: int) -> bool:
    minutes = _cooldown_minutes()
    if minutes <= 0:
        return False
    key = (task_id, person_id)
    last = _cooldown.get(key)
    if last is None:
        return False
    return datetime.now() - last < timedelta(minutes=minutes)


def _mark_sent(task_id: str, person_id: int) -> None:
    _cooldown[(task_id, person_id)] = datetime.now()


def _send_one(
    task_id: str,
    person_id: int,
    time_str: str,
    total_count: int,
    device_name: str = "",
) -> None:
    if not is_sms_enabled():
        return

    phones = _parse_phones()
    if not phones:
        logger.warning("SMS_PHONES 未配置，跳过短信 task=%s person=%s", task_id, person_id)
        return

    if _in_cooldown(task_id, person_id):
        logger.info(
            "短信冷却中，跳过 task=%s person=%s video_time=%s",
            task_id,
            person_id,
            time_str,
        )
        return

    content = _build_content(time_str, total_count, device_name)
    ok_any = False
    for phone in phones:
        if send_sms(phone, content):
            ok_any = True
            logger.info(
                "安全帽预警短信已发送 phone=%s task=%s person=%s",
                phone,
                task_id,
                person_id,
            )
        else:
            logger.warning(
                "安全帽预警短信发送失败 phone=%s task=%s person=%s",
                phone,
                task_id,
                person_id,
            )

    if ok_any:
        _mark_sent(task_id, person_id)


def _worker_loop() -> None:
    while True:
        task_id, person_id, time_str, total_count, device_name = _queue.get()
        try:
            _send_one(task_id, person_id, time_str, total_count, device_name)
        except Exception as e:
            logger.error("短信通知线程异常: %s", e)
        finally:
            _queue.task_done()


def notify_new_violation(
    task_id: str,
    person_id: int,
    time_str: str,
    *,
    total_count: int = 1,
    device_name: str = "",
) -> None:
    """检测到新的未戴安全帽人员时调用（非阻塞）。"""
    if not is_sms_enabled():
        return
    _ensure_worker()
    _queue.put((task_id, person_id, time_str, total_count, device_name or ""))
