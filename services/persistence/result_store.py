# -*- coding: utf-8 -*-
"""Persist and reload RTSP session detection results from disk."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from services.analysis.violation_store import _frame_to_time_str

RESULT_FILENAME = "session_result.json"
_FRAME_RE = re.compile(r"^f(\d+)\.jpg$", re.I)
_PERSON_RE = re.compile(r"^p(\d+)$", re.I)


def result_file_path(output_dir: Path) -> Path:
    return Path(output_dir) / RESULT_FILENAME


def save_session_result(output_dir: str | Path, result: Optional[Dict[str, Any]]) -> None:
    if not result:
        return
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = result_file_path(out)
    payload = {k: v for k, v in result.items() if k != "status_note"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_session_result(output_dir: str | Path) -> Optional[Dict[str, Any]]:
    path = result_file_path(Path(output_dir))
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _crop_url(task_id: str, rel: str) -> str:
    return f"/api/v1/tasks/{task_id}/files/crops/{rel}"


def rebuild_result_from_crops(
    task_id: str,
    output_dir: str | Path,
    *,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
    default_fps: float = 25.0,
) -> Dict[str, Any]:
    """Rebuild a minimal result snapshot by scanning crops/ when JSON is missing."""
    crops_dir = Path(output_dir) / "crops"
    images = []
    raw_count = 0
    if crops_dir.is_dir():
        for person_dir in sorted(crops_dir.iterdir()):
            if not person_dir.is_dir():
                continue
            m = _PERSON_RE.match(person_dir.name)
            if not m:
                continue
            person_id = int(m.group(1))
            members = []
            for jpg in sorted(person_dir.glob("*.jpg")):
                fm = _FRAME_RE.match(jpg.name)
                if not fm:
                    continue
                frame_index = int(fm.group(1))
                rel = f"{person_dir.name}/{jpg.name}"
                members.append(
                    {
                        "filename": rel,
                        "frame_index": frame_index,
                        "time_str": _frame_to_time_str(frame_index, default_fps),
                        "time_sec": round(frame_index / max(default_fps, 1.0), 2),
                        "confidence": 0.0,
                        "sim_to_best": 1.0,
                        "is_best": False,
                        "url": _crop_url(task_id, rel),
                    }
                )
                raw_count += 1
            if not members:
                continue
            members.sort(key=lambda x: x["frame_index"])
            best = max(members, key=lambda x: x["frame_index"])
            best["is_best"] = True
            cover = best
            images.append(
                {
                    "filename": cover["filename"],
                    "person_id": person_id,
                    "frame_index": cover["frame_index"],
                    "time_str": cover["time_str"],
                    "time_sec": cover["time_sec"],
                    "confidence": cover["confidence"],
                    "merged_count": len(members),
                    "url": cover["url"],
                    "merged_images": members,
                }
            )
        images.sort(key=lambda x: (x.get("time_sec", 0), x.get("frame_index", 0)))

    return {
        "saved_count": len(images),
        "violation_frames": raw_count,
        "sampled_frames": 0,
        "processed_frames": 0,
        "raw_count": raw_count,
        "images": images,
        "frame_stride": frame_stride,
        "enable_reid": enable_reid,
        "reid_threshold": reid_threshold,
    }


def resolve_session_result(
    task_id: str,
    output_dir: str | Path,
    *,
    frame_stride: int = 25,
    enable_reid: bool = True,
    reid_threshold: float = 0.7,
    live_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if live_result and live_result.get("images") is not None:
        return live_result
    saved = load_session_result(output_dir)
    if saved:
        return saved
    return rebuild_result_from_crops(
        task_id,
        output_dir,
        frame_stride=frame_stride,
        enable_reid=enable_reid,
        reid_threshold=reid_threshold,
    )


def result_summary(output_dir: str | Path) -> tuple[int, int]:
    saved = load_session_result(output_dir)
    if saved:
        return int(saved.get("saved_count") or 0), int(saved.get("violation_frames") or 0)
    crops_dir = Path(output_dir) / "crops"
    if not crops_dir.is_dir():
        return 0, 0
    persons = 0
    raw = 0
    for person_dir in crops_dir.iterdir():
        if person_dir.is_dir() and _PERSON_RE.match(person_dir.name):
            jpgs = list(person_dir.glob("*.jpg"))
            if jpgs:
                persons += 1
                raw += len(jpgs)
    return persons, raw
