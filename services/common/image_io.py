# -*- coding: utf-8 -*-
"""OpenCV I/O helpers (Unicode-safe paths on Windows)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def imwrite_path(path: Path | str, img: np.ndarray) -> bool:
    """Write BGR image; cv2.imwrite fails on non-ASCII paths on Windows."""
    path = Path(path)
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())
    return True
