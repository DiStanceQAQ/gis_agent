from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(slots=True)
class FieldHistoryFeatures:
    correction_count: int = 0
    accepted_count: int = 0
    contradiction_count: int = 0
    ignored_prompt_count: int = 0
    last_confirmed_revision_number: int | None = None

    def dump(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ConfidenceEvidence:
    source: str
    weight: float
    detail: str


@dataclass(slots=True)
class ConfidenceComputation:
    score: float
    level: Literal["low", "medium", "high"]
    evidence: list[ConfidenceEvidence]


def build_history_features(
    field_value_history: dict[str, list[dict[str, object]]] | None,
) -> dict[str, FieldHistoryFeatures]:
    features: dict[str, FieldHistoryFeatures] = {}
    for field_name, entries in dict(field_value_history or {}).items():
        if not isinstance(entries, list) or not entries:
            continue
        ordered = sorted(
            [entry for entry in entries if isinstance(entry, dict)],
            key=lambda item: int(item.get("revision_number") or 0),
            reverse=True,
        )
        if not ordered:
            continue
        correction_count = max(0, len(ordered) - 1)
        contradiction_count = max(0, len({_normalize_value(item.get("value")) for item in ordered}) - 1)
        last_confirmed_revision_number = _coerce_int(ordered[0].get("revision_number"))
        features[field_name] = FieldHistoryFeatures(
            correction_count=correction_count,
            accepted_count=len(ordered),
            contradiction_count=contradiction_count,
            last_confirmed_revision_number=last_confirmed_revision_number,
        )
    return features


def compute_field_confidence(
    *,
    field_name: str,
    parser_score: float,
    history_features: dict[str, dict[str, object]] | dict[str, FieldHistoryFeatures],
    current_signal_score: float,
    high_threshold: float = 0.85,
    medium_threshold: float = 0.60,
) -> ConfidenceComputation:
    feature = _coerce_feature(history_features.get(field_name))
    score = parser_score * 0.45 + current_signal_score * 0.35
    score += min(feature.accepted_count, 4) * 0.08
    score += min(feature.correction_count, 4) * 0.04
    score -= min(feature.contradiction_count, 4) * 0.07
    score -= min(feature.ignored_prompt_count, 4) * 0.04
    if feature.last_confirmed_revision_number is not None:
        score += 0.02
    score = max(0.0, min(1.0, score))

    if score >= high_threshold:
        level: Literal["low", "medium", "high"] = "high"
    elif score >= medium_threshold:
        level = "medium"
    else:
        level = "low"

    return ConfidenceComputation(
        score=score,
        level=level,
        evidence=[
            ConfidenceEvidence(
                source="parser",
                weight=parser_score,
                detail="parser candidate confidence",
            ),
            ConfidenceEvidence(
                source="current_signals",
                weight=current_signal_score,
                detail="current message signals",
            ),
            ConfidenceEvidence(
                source="revision_history",
                weight=float(feature.accepted_count),
                detail=(
                    "accepted="
                    f"{feature.accepted_count}, corrected={feature.correction_count}, contradicted={feature.contradiction_count}"
                ),
            ),
        ],
    )


def _coerce_feature(value: object) -> FieldHistoryFeatures:
    if isinstance(value, FieldHistoryFeatures):
        return value
    if isinstance(value, dict):
        return FieldHistoryFeatures(
            correction_count=_coerce_int(value.get("correction_count")) or 0,
            accepted_count=_coerce_int(value.get("accepted_count")) or 0,
            contradiction_count=_coerce_int(value.get("contradiction_count")) or 0,
            ignored_prompt_count=_coerce_int(value.get("ignored_prompt_count")) or 0,
            last_confirmed_revision_number=_coerce_int(value.get("last_confirmed_revision_number")),
        )
    return FieldHistoryFeatures()


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_value(value: object) -> str:
    return repr(value)
