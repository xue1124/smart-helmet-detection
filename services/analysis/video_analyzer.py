# -*- coding: utf-8 -*-
"""Video frame extraction with decord and incremental no-helmet export."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np
from decord import VideoReader, cpu

from services.analysis.helmet_detector import HelmetDetector, crop_box_for_violation
from services.analysis.violation_store import LiveResultStore


def _frame_to_time_str(frame_idx: int, fps: float) -> str:
    if fps <= 0:
        fps = 25.0
    total_sec = frame_idx / fps
    hours = int(total_sec // 3600)
    minutes = int((total_sec % 3600) // 60)
    seconds = int(total_sec % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _crop_region(im0: np.ndarray, box: List[float]) -> np.ndarray:
    h, w = im0.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return im0
    return im0[y1:y2, x1:x2].copy()


class OfflineVideoAnalyzer:
    """Sample video frames, detect violations, incrementally merge via OSNet."""

    def __init__(self, detector: HelmetDetector, output_dir: Path, task_id: str = ""):
        self.detector = detector
        self.output_dir = Path(output_dir)
        self.task_id = task_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "crops").mkdir(exist_ok=True)

    def process(
        self,
        video_path: str,
        frame_stride: int = 25,
        enable_reid: bool = True,
        reid_threshold: float = 0.7,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        result_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        frame_stride = max(1, int(frame_stride))
        reid_threshold = float(reid_threshold)
        t0 = time.time()

        vr = VideoReader(video_path, ctx=cpu(0))
        total_frames = len(vr)
        try:
            fps = float(vr.get_avg_fps())
        except Exception:
            fps = 25.0
        if fps <= 0:
            fps = 25.0

        indices = list(range(0, total_frames, frame_stride))
        total_sampled = len(indices)

        store = LiveResultStore(
            self.task_id,
            self.output_dir,
            enable_reid=enable_reid,
            reid_threshold=reid_threshold,
        )
        store.frame_stride = frame_stride

        def push_result() -> None:
            if result_callback is None:
                return
            snap = store.snapshot()
            snap["elapsed_sec"] = round(time.time() - t0, 2)
            snap["fps"] = round(fps, 2)
            snap["total_frames"] = total_frames
            snap["sampled_frames"] = total_sampled
            result_callback(snap)

        push_result()

        for i, frame_idx in enumerate(indices):
            if cancel_check and cancel_check():
                break

            frame = vr[frame_idx].asnumpy()
            im0 = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            det = self.detector.predict(im0)
            violations = self.detector.find_no_helmet_heads(det)
            had_violation = bool(violations)
            store.tick_frame(violation=had_violation)

            if violations:
                for head_box, head_conf in violations:
                    person_box = self.detector.find_person_for_head(det, head_box)
                    crop_box = crop_box_for_violation(head_box, person_box, im0.shape)
                    crop = _crop_region(im0, crop_box)
                    store.add_detection(crop, frame_idx, fps, head_conf)
                push_result()

            if progress_callback:
                progress_callback(
                    i + 1,
                    total_sampled,
                    f"frame {frame_idx}/{total_frames}, persons={store.snapshot()['saved_count']}",
                )

            if (i + 1) % 10 == 0:
                push_result()

        push_result()

        result = store.snapshot()
        result.update(
            {
                "video_path": video_path,
                "total_frames": total_frames,
                "sampled_frames": total_sampled,
                "fps": round(fps, 2),
                "elapsed_sec": round(time.time() - t0, 2),
            }
        )
        return result
