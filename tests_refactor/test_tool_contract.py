import asyncio

from core.tool_system import Tool


def test_tool_wraps_dict_once_and_propagates_logical_failure():
    async def operation():
        return {"success": False, "error": "expected", "payload": 1}

    tool = Tool("test", "test", operation, {"type": "object", "properties": {}}, max_retries=1)
    result = asyncio.run(tool.execute())
    assert result.success is False
    assert result.error == "expected"
    assert result.data == {"success": False, "error": "expected", "payload": 1}


def test_registry_has_all_eleven_tools_and_memory_identity_schema():
    from core.tool_system import create_tool_registry

    registry = create_tool_registry()
    assert set(registry.list_tool_names()) == {
        "search_properties", "calculate_commute", "calculate_commute_cost",
        "check_safety", "get_weather", "web_search", "search_nearby_pois",
        "get_property_details", "check_transport_cost", "recall_memory", "remember",
    }
    for name in ("recall_memory", "remember"):
        properties = registry.get(name).parameters["properties"]
        assert {"user_id", "session_id"} <= set(properties)


def test_agent_state_carries_identity():
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("hello", user_id="user-a", session_id="session-a")
    assert state["user_id"] == "user-a"
    assert state["session_id"] == "session-a"
