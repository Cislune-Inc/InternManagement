from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def build_model_candidates(*models: str | None) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for model in models:
        if not model:
            continue
        normalized = str(model).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


class ModelFallbackChain:
    def __init__(self, label: str, *models: str | None) -> None:
        self.label = label
        self.models = build_model_candidates(*models)
        self.active_model = self.models[0] if self.models else None

    def candidate_models(self) -> list[str]:
        if not self.models:
            return []
        if self.active_model and self.active_model in self.models:
            return [self.active_model] + [model for model in self.models if model != self.active_model]
        return list(self.models)

    def record_success(self, model: str) -> None:
        if model == self.active_model:
            return
        logger.warning(
            "Switching %s model from %s to %s after previous failures.",
            self.label,
            self.active_model or "unconfigured",
            model,
        )
        self.active_model = model
