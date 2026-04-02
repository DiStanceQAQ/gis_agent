import pytest

from packages.domain.services.tool_registry import (
    TOOL_DEFINITIONS,
    TOOL_STEP_SEQUENCE,
    get_tool_definition,
    list_tool_definitions,
    require_tool_definition,
)


def test_list_tool_definitions_follows_step_sequence() -> None:
    definitions = list_tool_definitions()

    assert [tool.step_name for tool in definitions] == TOOL_STEP_SEQUENCE
    assert len({tool.tool_name for tool in definitions}) == len(definitions)


def test_tool_contracts_define_input_and_output_schema() -> None:
    for step_name, tool in TOOL_DEFINITIONS.items():
        assert tool.step_name == step_name
        assert tool.contract.input_schema
        assert tool.contract.output_schema


def test_get_and_require_tool_definition() -> None:
    normalize_tool = get_tool_definition("normalize_aoi")

    assert normalize_tool is not None
    assert normalize_tool.tool_name == "aoi.normalize"
    assert require_tool_definition("search_candidates").tool_name == "catalog.search"

    assert get_tool_definition("missing_step") is None
    with pytest.raises(KeyError):
        require_tool_definition("missing_step")
