# -*- coding: utf-8 -*-
"""OSNet person re-identification feature extractor (ONNX)."""

from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from services.config import WEIGHTS_DIR

# ImageNet normalization (torchreid / OSNet standard)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Person ReID: height=256, width=128
_INPUT_H = 256
_INPUT_W = 128


def _ort_version_tuple() -> Tuple[int, int]:
    import onnxruntime as ort

    parts = ort.__version__.split(".")[:2]
    return int(parts[0]), int(parts[1])


def _onnx_model_candidates() -> List[Path]:
    """Return model paths to try, ordered by onnxruntime compatibility."""
    ir9 = [
        WEIGHTS_DIR / "osnet_ain_msmt17_dynamic_ir9.onnx",
    ]
    standard = [
        WEIGHTS_DIR / "osnet_ain_msmt17_dynamic.onnx",
    ]
    # onnxruntime-gpu <= 1.16 only supports ONNX IR <= 9
    if _ort_version_tuple() < (1, 17):
        return ir9 + standard
    return standard + ir9


def default_onnx_path() -> Path:
    for p in _onnx_model_candidates():
        if p.exists():
            return p
    raise FileNotFoundError(
        "OSNet ONNX not found. Place osnet_ain_msmt17_dynamic.onnx "
        "(or osnet_ain_msmt17_dynamic_ir9.onnx for onnxruntime<=1.16) in weights/ or project root."
    )


def _create_session(model_path: Path, use_gpu: bool):
    import onnxruntime as ort

    provider_chain: List[str] = []
    if use_gpu and "CUDAExecutionProvider" in ort.get_available_providers():
        provider_chain.append("CUDAExecutionProvider")
    provider_chain.append("CPUExecutionProvider")

    try:
        return ort.InferenceSession(str(model_path), providers=provider_chain)
    except Exception as gpu_err:
        if "CUDAExecutionProvider" not in provider_chain:
            raise
        # CUDA DLL / driver mismatch: fall back to CPU-only session
        return ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])


def _load_session(model_path: Optional[str], use_gpu: bool):
    import onnxruntime as ort

    if model_path:
        paths = [Path(model_path)]
    else:
        paths = [p for p in _onnx_model_candidates() if p.exists()]

    if not paths:
        raise FileNotFoundError(
            "OSNet ONNX not found. Place osnet_ain_msmt17_dynamic.onnx in weights/ or project root."
        )

    last_err: Optional[Exception] = None
    for path in paths:
        try:
            session = _create_session(path, use_gpu)
            return session, str(path)
        except Exception as e:
            last_err = e
            msg = str(e)
            # IR version mismatch: try next candidate (e.g. *_ir9.onnx)
            if "IR version" in msg or "Unsupported model" in msg:
                continue
            raise

    raise RuntimeError(
        f"Failed to load OSNet ONNX with onnxruntime {ort.__version__}. "
        f"Tried: {[str(p) for p in paths]}. Last error: {last_err}"
    )


class OsnetReidExtractor:
    """Load OSNet ONNX once and extract L2-normalized appearance features."""

    def __init__(self, model_path: Optional[str] = None, use_gpu: bool = True):
        self.session, self.model_path = _load_session(model_path, use_gpu)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def preprocess(self, crop_bgr: np.ndarray) -> np.ndarray:
        """BGR crop -> NCHW float32 tensor [1,3,H,W]."""
        img = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (_INPUT_W, _INPUT_H), interpolation=cv2.INTER_LINEAR)
        img = img.astype(np.float32) / 255.0
        img = (img - _MEAN) / _STD
        img = img.transpose(2, 0, 1)[np.newaxis, ...]
        return np.ascontiguousarray(img)

    def extract(self, crop_bgr: np.ndarray) -> np.ndarray:
        feat = self.extract_batch([crop_bgr])[0]
        return feat

    def extract_batch(self, crops_bgr: List[np.ndarray]) -> np.ndarray:
        if not crops_bgr:
            return np.zeros((0, 512), dtype=np.float32)

        batch = np.concatenate([self.preprocess(c) for c in crops_bgr], axis=0)
        out = self.session.run([self.output_name], {self.input_name: batch})[0]
        feats = np.asarray(out, dtype=np.float32)
        if feats.ndim == 1:
            feats = feats[np.newaxis, :]
        # L2 normalize for cosine similarity via dot product
        norms = np.linalg.norm(feats, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        return feats / norms


_reid: Optional[OsnetReidExtractor] = None


def get_reid_extractor() -> OsnetReidExtractor:
    global _reid
    if _reid is None:
        _reid = OsnetReidExtractor()
    return _reid
