from packages.domain.utils import merge_dicts


def test_merge_dicts_recursively() -> None:
    base = {"time_range": {"start": "2024-06-01", "end": "2024-08-31"}, "analysis_type": "NDVI"}
    override = {"time_range": {"start": "2023-06-01"}}

    merged = merge_dicts(base, override)

    assert merged["time_range"]["start"] == "2023-06-01"
    assert merged["time_range"]["end"] == "2024-08-31"
    assert merged["analysis_type"] == "NDVI"

