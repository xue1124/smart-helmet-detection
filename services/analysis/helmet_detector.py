# -*- coding: utf-8 -*-
"""Pure YOLO inference engine (no PyQt dependency)."""

import random
from typing import List, Optional, Tuple

import cv2
import numpy as np
import torch

from models.experimental import attempt_load
from utils import torch_utils
from utils.datasets import letterbox
from utils.utils import check_img_size, non_max_suppression, plot_one_box, scale_coords

from services.config import WEIGHTS_DIR


def _box_iou(box_a, box_b) -> float:
    """IoU between two xyxy boxes."""
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _expand_box(xyxy, shape, ratio=0.15):
    h, w = shape[:2]
    x1, y1, x2, y2 = xyxy
    bw, bh = x2 - x1, y2 - y1
    pad_w, pad_h = bw * ratio, bh * ratio
    return (
        max(0, int(x1 - pad_w)),
        max(0, int(y1 - pad_h)),
        min(w, int(x2 + pad_w)),
        min(h, int(y2 + pad_h)),
    )


def _union_boxes(*boxes: Optional[List[float]]) -> Optional[List[float]]:
    valid = [b for b in boxes if b]
    if not valid:
        return None
    return [
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    ]


def crop_box_for_violation(
    head_box: List[float],
    person_box: Optional[List[float]],
    shape,
    ratio: float = 0.15,
) -> tuple:
    """Union head + person, expand padding — keeps full upper body in crop."""
    base = _union_boxes(person_box, head_box) or head_box
    return _expand_box(base, shape, ratio=ratio)


class HelmetDetector:
    """Loads YOLO weights once and runs frame-level detection."""

    def __init__(
        self,
        weights: List[str],
        device: str = "",
        img_size: int = 640,
        conf_thres: float = 0.4,
        iou_thres: float = 0.5,
        helmet_iou_thresh: float = 0.25,
    ):
        self.weights = weights
        self.img_size = img_size
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.helmet_iou_thresh = helmet_iou_thresh

        self.device = torch_utils.select_device(device)
        self.half = self.device.type != "cpu"
        self.model = attempt_load(weights, map_location=self.device)
        self.img_size = check_img_size(img_size, s=self.model.stride.max())
        if self.half:
            self.model.half()

        self.names = (
            self.model.module.names
            if hasattr(self.model, "module")
            else self.model.names
        )
        self.colors = [
            [random.randint(0, 255) for _ in range(3)]
            for _ in range(len(self.names))
        ]
        self._name_to_idx = {n: i for i, n in enumerate(self.names)}

        img = torch.zeros((1, 3, self.img_size, self.img_size), device=self.device)
        _ = self.model(img.half() if self.half else img)
        if self.device.type != "cpu":
            torch.cuda.synchronize()

    def preprocess(self, im0: np.ndarray) -> torch.Tensor:
        img = letterbox(im0, new_shape=self.img_size)[0]
        img = img[:, :, ::-1].transpose(2, 0, 1)
        img = np.ascontiguousarray(img)
        img = torch.from_numpy(img).to(self.device)
        img = img.half() if self.half else img.float()
        img /= 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        return img

    @torch.no_grad()
    def predict(self, im0: np.ndarray) -> torch.Tensor:
        """Return detections tensor [N, 6] in original image coords (xyxy, conf, cls)."""
        img = self.preprocess(im0)
        pred = self.model(img, augment=False)[0]
        pred = non_max_suppression(
            pred,
            self.conf_thres,
            self.iou_thres,
            classes=None,
            agnostic=False,
        )
        det = pred[0]
        if det is not None and len(det):
            det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()
        return det

    def find_no_helmet_heads(
        self, det: Optional[torch.Tensor]
    ) -> List[Tuple[List[float], float]]:
        """
        Find head boxes without overlapping helmet.
        Returns list of (xyxy, confidence).
        """
        if det is None or len(det) == 0:
            return []

        head_idx = self._name_to_idx.get("head")
        helmet_idx = self._name_to_idx.get("helmet")
        if head_idx is None:
            return []

        heads = []
        helmets = []
        for *xyxy, conf, cls in det:
            c = int(cls.item()) if hasattr(cls, "item") else int(cls)
            box = [float(x) for x in xyxy]
            cf = float(conf.item()) if hasattr(conf, "item") else float(conf)
            if c == head_idx:
                heads.append((box, cf))
            elif helmet_idx is not None and c == helmet_idx:
                helmets.append(box)

        violations = []
        for head_box, head_conf in heads:
            has_helmet = any(
                _box_iou(head_box, h) >= self.helmet_iou_thresh for h in helmets
            )
            if not has_helmet:
                violations.append((head_box, head_conf))
        return violations

    def find_person_for_head(
        self, det: Optional[torch.Tensor], head_box: List[float]
    ) -> Optional[List[float]]:
        """Return person box containing head center, if any."""
        if det is None or len(det) == 0:
            return None
        person_idx = self._name_to_idx.get("person")
        if person_idx is None:
            return None

        cx = (head_box[0] + head_box[2]) / 2
        cy = (head_box[1] + head_box[3]) / 2
        best, best_area = None, float("inf")
        for *xyxy, conf, cls in det:
            c = int(cls.item()) if hasattr(cls, "item") else int(cls)
            if c != person_idx:
                continue
            box = [float(x) for x in xyxy]
            if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                area = (box[2] - box[0]) * (box[3] - box[1])
                if area < best_area:
                    best_area = area
                    best = box
        return best

    def draw_detections(
        self, im0: np.ndarray, det: Optional[torch.Tensor], highlight_heads: List[List[float]] = None
    ) -> np.ndarray:
        out = im0.copy()
        highlight_heads = highlight_heads or []
        if det is None:
            return out
        for *xyxy, conf, cls in det:
            c = int(cls.item()) if hasattr(cls, "item") else int(cls)
            box = [float(x) for x in xyxy]
            is_violation = any(_box_iou(box, h) > 0.5 for h in highlight_heads)
            color = (0, 0, 255) if is_violation else self.colors[c]
            label = f"{self.names[c]} {float(conf):.2f}"
            if is_violation:
                label = "no_helmet " + label
            plot_one_box(xyxy, out, label=label, color=color, line_thickness=2)
        return out

    @staticmethod
    def default_weights() -> List[str]:
        model_path = WEIGHTS_DIR / "helmet_head_person_l.pt"
        if not model_path.exists():
            raise FileNotFoundError(f"Helmet detector weights not found: {model_path}")
        return [str(model_path)]
