"""Пути к изображениям Т2 (арт и наборы выбора)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def art_image_path(art_id: int) -> Path:
    return _ROOT / "images" / "art" / f"{int(art_id)}.jpg"


def choice_image_path(choice_set_id: int, image_id: int) -> Path:
    return _ROOT / "images" / "choice_sets" / str(int(choice_set_id)) / f"{int(image_id)}.jpg"
