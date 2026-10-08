"""Shared filesystem locations used by the backend."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = PROJECT_ROOT / "weights"
RUNTIME_DIR = PROJECT_ROOT / "inference"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
OUTPUT_DIR = RUNTIME_DIR / "no_helmet_tasks"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
