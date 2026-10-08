"""Run helmet detection on a local video and save an annotated preview."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.analysis.helmet_detector import HelmetDetector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="input video path")
    parser.add_argument("output", type=Path, help="annotated output video path")
    parser.add_argument("--frame-stride", type=int, default=25, help="infer every N frames")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(f"Input video not found: {args.input}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    detector = HelmetDetector(HelmetDetector.default_weights())
    capture = cv2.VideoCapture(str(args.input))
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video: {args.input}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    writer = cv2.VideoWriter(
        str(args.output),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    frame_index = 0
    stride = max(1, args.frame_stride)
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index % stride == 0:
                detections = detector.predict(frame)
                frame = detector.draw_detections(frame, detections)
            writer.write(frame)
            frame_index += 1
            if frame_index % 100 == 0 and total_frames > 0:
                print(f"{frame_index}/{total_frames} ({frame_index / total_frames:.1%})")
    finally:
        capture.release()
        writer.release()

    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
