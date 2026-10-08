# -*- coding: utf-8 -*-
"""Incremental OSNet merge for live RTSP detections."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from services.common.image_io import imwrite_path
from services.analysis.person_reid import OsnetReidExtractor, get_reid_extractor


def _frame_to_time_str(frame_idx: int, fps: float) -> str:
    if fps <= 0:
        fps = 25.0
    total_sec = frame_idx / fps
    hours = int(total_sec // 3600)
    minutes = int((total_sec % 3600) // 60)
    seconds = int(total_sec % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class LiveResultStore:
    """Merge each new crop into existing persons incrementally."""

    def __init__(
        self,
        task_id: str,
        out_dir: Path,
        enable_reid: bool = True,
        reid_threshold: float = 0.7,
        reid: Optional[OsnetReidExtractor] = None,
        sms_notify: bool = False,
        device_name: str = "",
    ):
        self.task_id = task_id
        self.out_dir = Path(out_dir)
        self.crops_dir = self.out_dir / "crops"
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.enable_reid = enable_reid
        self.reid_threshold = float(reid_threshold)
        self.reid = reid
        self.sms_notify = sms_notify
        self.device_name = (device_name or "").strip()

        self._lock = threading.Lock()
        self._clusters: List[Dict[str, Any]] = []
        self.violation_frames = 0
        self.processed_frames = 0
        self.raw_count = 0
        self.frame_stride = 1

    def _reid_engine(self) -> OsnetReidExtractor:
        if self.reid is None:
            self.reid = get_reid_extractor()
        return self.reid

    def _crop_url(self, rel: str) -> str:
        return f"/api/v1/tasks/{self.task_id}/files/crops/{rel}"

    def _export_members(self, members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{k: v for k, v in m.items() if k != "feat"} for m in members]

    def _latest_member(self, members: List[Dict[str, Any]]) -> Dict[str, Any]:
        return max(members, key=lambda m: (m.get("time_sec", 0), m.get("frame_index", 0)))

    def _cluster_image_record(self, cluster: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not cluster["merged_images"]:
            return None
        exported = self._export_members(cluster["merged_images"])
        cover_pub = next(
            (m for m in exported if m.get("is_best")),
            exported[-1],
        )
        latest = self._latest_member(exported)
        return {
            "filename": cover_pub["filename"],
            "person_id": cluster["person_id"],
            "frame_index": latest["frame_index"],
            "time_str": latest["time_str"],
            "time_sec": latest["time_sec"],
            "confidence": cover_pub["confidence"],
            "merged_count": len(exported),
            "url": cover_pub["url"],
            "merged_images": exported,
        }

    def add_detection(
        self,
        crop_bgr: np.ndarray,
        frame_index: int,
        fps: float,
        confidence: float,
    ) -> Dict[str, Any]:
        time_str = _frame_to_time_str(frame_index, fps)
        time_sec = round(frame_index / max(fps, 1.0), 2)
        feat: Optional[np.ndarray] = None

        if self.enable_reid:
            feat = self._reid_engine().extract(crop_bgr)

        is_new_person = False
        notify_person_id: Optional[int] = None

        with self._lock:
            self.raw_count += 1
            cluster_idx = self._match_cluster(feat)
            is_new_person = cluster_idx is None

            if is_new_person:
                person_id = len(self._clusters)
                cluster = {
                    "person_id": person_id,
                    "best_frame": frame_index,
                    "best_conf": confidence,
                    "merged_images": [],
                }
                self._clusters.append(cluster)
                cluster_idx = person_id
                notify_person_id = person_id
            else:
                cluster = self._clusters[cluster_idx]
                if confidence > cluster["best_conf"]:
                    cluster["best_frame"] = frame_index
                    cluster["best_conf"] = confidence

            person_id = cluster["person_id"]
            person_dir = self.crops_dir / f"p{person_id:03d}"
            person_dir.mkdir(parents=True, exist_ok=True)
            fname = f"f{frame_index:06d}.jpg"
            rel = f"p{person_id:03d}/{fname}"
            if not imwrite_path(person_dir / fname, crop_bgr):
                raise RuntimeError(f"无法保存截图: {person_dir / fname}")

            sim = 1.0
            if feat is not None and cluster["merged_images"]:
                sims = [
                    float(np.dot(feat, m["feat"]))
                    for m in cluster["merged_images"]
                    if m.get("feat") is not None
                ]
                if sims:
                    sim = round(max(sims), 3)

            is_best = frame_index == cluster["best_frame"] and abs(confidence - cluster["best_conf"]) < 1e-5
            mem: Dict[str, Any] = {
                "filename": rel,
                "frame_index": frame_index,
                "time_str": time_str,
                "time_sec": time_sec,
                "confidence": round(float(confidence), 3),
                "sim_to_best": sim,
                "is_best": is_best,
                "url": self._crop_url(rel),
            }
            if feat is not None:
                mem["feat"] = feat

            existing = next((i for i, m in enumerate(cluster["merged_images"]) if m["frame_index"] == frame_index), None)
            if existing is not None:
                cluster["merged_images"][existing] = mem
            else:
                cluster["merged_images"].append(mem)
                cluster["merged_images"].sort(key=lambda x: x["frame_index"])

            for m in cluster["merged_images"]:
                m["is_best"] = (
                    m["frame_index"] == cluster["best_frame"]
                    and abs(float(m["confidence"]) - float(cluster["best_conf"])) < 1e-5
                )

            snapshot = self.snapshot_unlocked()
            total_count = len(self._clusters)

        if (
            is_new_person
            and self.sms_notify
            and self.enable_reid
            and notify_person_id is not None
        ):
            from services.integrations.sms_notifier import notify_new_violation

            notify_new_violation(
                self.task_id,
                notify_person_id,
                time_str,
                total_count=total_count,
                device_name=self.device_name,
            )

        return snapshot

    def _match_cluster(self, feat: Optional[np.ndarray]) -> Optional[int]:
        if not self.enable_reid or feat is None:
            return None
        best_idx = None
        best_sim = -1.0
        for i, cluster in enumerate(self._clusters):
            max_sim = -1.0
            for m in cluster["merged_images"]:
                mfeat = m.get("feat")
                if mfeat is None:
                    continue
                max_sim = max(max_sim, float(np.dot(feat, mfeat)))
            if max_sim >= self.reid_threshold and max_sim > best_sim:
                best_sim = max_sim
                best_idx = i
        return best_idx

    def tick_frame(self, violation: bool = False) -> None:
        with self._lock:
            self.processed_frames += 1
            if violation:
                self.violation_frames += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self.snapshot_unlocked()

    def snapshot_unlocked(self) -> Dict[str, Any]:
        images = []
        for cluster in self._clusters:
            rec = self._cluster_image_record(cluster)
            if rec:
                images.append(rec)
        images.sort(key=lambda x: (x.get("time_sec", 0), x.get("frame_index", 0)))
        return {
            "saved_count": len(self._clusters),
            "violation_frames": self.violation_frames,
            "sampled_frames": self.processed_frames,
            "processed_frames": self.processed_frames,
            "raw_count": self.raw_count,
            "images": images,
            "frame_stride": self.frame_stride,
            "enable_reid": self.enable_reid,
            "reid_threshold": self.reid_threshold,
        }
