"""
Общий загрузчик decision-модели для AutoShortService и MlShortService.

Логика выбора файла:
1. Если есть `models/decision_model_features.json` с полем `model_file` — берём pkl
   по этому имени (это последняя обученная модель из train_decision_model.py).
2. Иначе fallback — последний по mtime файл `models/decision_model_*.pkl`,
   игнорируя эксперименты с суффиксами `_v2` и `_nodead`.
3. Иначе старый legacy `models/decision_model.pkl` (если кто-то его руками положил).

Возвращает (model, features_list, source_path) или (None, fallback_features, None).
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

from app.utils.logging import get_logger

logger = get_logger(__name__)

MODELS_DIR = Path("models")
MANIFEST_PATH = MODELS_DIR / "decision_model_features.json"
LEGACY_MODEL_PATH = MODELS_DIR / "decision_model.pkl"


def _find_latest_pkl() -> Path | None:
    """Самый свежий decision_model_*.pkl, исключая эксперименты."""
    if not MODELS_DIR.exists():
        return None
    candidates: list[Path] = []
    for p in MODELS_DIR.glob("decision_model_*.pkl"):
        stem = p.stem  # decision_model_2026-05-10_202504
        # Отсекаем эксперименты
        if "_nodead_" in stem or stem.endswith("_nodead"):
            continue
        if "_v2_" in stem or stem.endswith("_v2"):
            continue
        candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_model_path() -> Path | None:
    """
    Определяет какой файл .pkl грузить. Приоритет: манифест → latest glob → legacy.
    Возвращает None если ничего не найдено.
    """
    # 1. Манифест
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            model_file = manifest.get("model_file")
            if model_file:
                p = MODELS_DIR / model_file
                if p.exists():
                    return p
        except Exception as exc:
            logger.warning("manifest read failed", error=str(exc))
    # 2. Самый свежий glob
    latest = _find_latest_pkl()
    if latest is not None:
        return latest
    # 3. Legacy
    if LEGACY_MODEL_PATH.exists():
        return LEGACY_MODEL_PATH
    return None


def load_decision_model(
    fallback_features: list[str],
) -> tuple[Any, list[str], Path | None]:
    """
    Грузит свежую decision-модель + список фичей.

    Возвращает (model_or_None, features_list, path_or_None).
    Никогда не raise — при ошибке возвращает (None, fallback_features, None).
    """
    path = resolve_model_path()
    if path is None:
        logger.warning(
            "decision model not found — ML-gate disabled (fail-open)",
            models_dir=str(MODELS_DIR),
        )
        return None, fallback_features, None

    try:
        with open(path, "rb") as f:
            model = pickle.load(f)
        logger.info("decision model loaded", path=str(path))
    except Exception as exc:
        logger.warning(
            "failed to load decision model",
            path=str(path),
            error=str(exc),
        )
        return None, fallback_features, None

    # Манифест фичей
    features = fallback_features
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            features = manifest["features"]
            logger.info(
                "features manifest loaded",
                n_features=len(features),
                path=str(MANIFEST_PATH),
            )
        except Exception as exc:
            logger.warning(
                "manifest read failed — fallback на хардкод",
                error=str(exc),
            )
    else:
        logger.warning(
            "features manifest not found — fallback на хардкод (возможен рассинхрон)",
            path=str(MANIFEST_PATH),
        )

    return model, features, path
