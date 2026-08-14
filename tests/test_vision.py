from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import vision


def _mock_content_response(text: str) -> MagicMock:
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = text
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "expected_reasoning"),
    [
        ("", None),
        ("none", {"effort": "none", "enabled": False}),
        ("low", {"effort": "low", "enabled": True}),
    ],
)
async def test_analyze_image_reasoning_extra_body(effort, expected_reasoning):
    async_create_mock = AsyncMock(return_value=_mock_content_response("a cat"))
    with (
        patch.object(vision, "LLM_VISION_REASONING_EFFORT", effort),
        patch.object(vision.client.chat.completions, "create", async_create_mock),
    ):
        assert await vision.analyze_image(b"fake-bytes") == "a cat"

    extra_body = async_create_mock.call_args.kwargs["extra_body"]
    if expected_reasoning is None:
        assert "reasoning" not in extra_body
    else:
        assert extra_body["reasoning"] == expected_reasoning
    assert extra_body["safetySettings"]


@pytest.mark.asyncio
async def test_analyze_frames_omits_reasoning_by_default():
    async_create_mock = AsyncMock(return_value=_mock_content_response("dancing"))
    with (
        patch.object(vision, "LLM_VISION_REASONING_EFFORT", ""),
        patch.object(vision.client.chat.completions, "create", async_create_mock),
    ):
        assert await vision.analyze_frames([b"frame"]) == "dancing"

    extra_body = async_create_mock.call_args.kwargs["extra_body"]
    assert "reasoning" not in extra_body
    assert extra_body["safetySettings"]
