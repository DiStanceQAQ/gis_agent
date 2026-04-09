from __future__ import annotations

from packages.domain.services.session_memory_confidence import (
    build_history_features,
    compute_field_confidence,
)


def test_field_confidence_uses_revision_history_features() -> None:
    features = {
        "aoi_input": {
            "correction_count": 3,
            "accepted_count": 3,
            "contradiction_count": 0,
            "last_confirmed_revision_number": 4,
        }
    }

    score = compute_field_confidence(
        field_name="aoi_input",
        parser_score=0.62,
        history_features=features,
        current_signal_score=0.75,
    )

    assert score.score > 0.75
    assert any(item.source == "revision_history" for item in score.evidence)


def test_build_history_features_from_field_value_history_tracks_repeated_corrections() -> None:
    features = build_history_features(
        {
            "time_range": [
                {"revision_id": "rev_3", "revision_number": 3, "value": {"start": "2023-06-01", "end": "2023-06-30"}},
                {"revision_id": "rev_2", "revision_number": 2, "value": {"start": "2024-06-01", "end": "2024-06-30"}},
                {"revision_id": "rev_1", "revision_number": 1, "value": {"start": "2023-06-01", "end": "2023-06-30"}},
            ]
        }
    )

    assert features["time_range"].correction_count == 2
    assert features["time_range"].contradiction_count == 1
    assert features["time_range"].last_confirmed_revision_number == 3
