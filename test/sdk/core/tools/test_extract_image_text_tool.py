from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sdk.nexent.core.tools import extract_image_text_tool
from sdk.nexent.core.tools.extract_image_text_tool import ExtractImageTextTool
from sdk.nexent.core.utils.observer import MessageObserver


@pytest.fixture
def observer_en():
    observer = MagicMock(spec=MessageObserver)
    observer.lang = "en"
    return observer


@pytest.fixture
def mock_vlm_model():
    return MagicMock()


@pytest.fixture
def mock_prompt_loader(monkeypatch):
    calls = []

    def _fake_get_prompt(template_type, language=None, **_):
        calls.append((template_type, language))
        return {"system_prompt": "Transcribe exactly."}

    monkeypatch.setattr(
        extract_image_text_tool,
        "get_prompt_template",
        _fake_get_prompt,
    )
    return calls


@pytest.fixture
def tool(observer_en, mock_vlm_model):
    return ExtractImageTextTool(
        observer=observer_en,
        vlm_model=mock_vlm_model,
        storage_client=MagicMock(),
    )


def test_forward_impl_transcribes_multiple_images_in_order(
    tool, mock_vlm_model, mock_prompt_loader
):
    mock_vlm_model.analyze_image.side_effect = [
        SimpleNamespace(content="A\nA\nB"),
        SimpleNamespace(content="print('x')"),
    ]

    result = tool._forward_impl([b"first", b"second"])

    assert result == ["A\nA\nB", "print('x')"]
    assert mock_prompt_loader == [("extract_image_text", "en")]
    assert mock_vlm_model.analyze_image.call_count == 2
    for call in mock_vlm_model.analyze_image.call_args_list:
        assert call.kwargs["temperature"] == 0
        assert call.kwargs["system_prompt"] == "Transcribe exactly."
        assert call.kwargs["image_input"].read() in {b"first", b"second"}


@pytest.mark.parametrize(
    "image_list,error_message",
    [
        (None, "image_urls cannot be None"),
        ("not-a-list", "image_urls must be a list of bytes"),
        ([], "image_urls must contain at least one image"),
    ],
)
def test_forward_impl_validates_inputs(tool, image_list, error_message):
    with pytest.raises(ValueError, match=error_message):
        tool._forward_impl(image_list)


def test_forward_impl_requires_vlm(observer_en):
    tool = ExtractImageTextTool(
        observer=observer_en,
        vlm_model=None,
        storage_client=MagicMock(),
    )

    with pytest.raises(Exception, match="Image understanding model is not configured"):
        tool._forward_impl([b"image"])


def test_forward_impl_wraps_model_error(
    tool, mock_vlm_model, mock_prompt_loader
):
    mock_vlm_model.analyze_image.side_effect = RuntimeError("model failed")

    with pytest.raises(
        Exception,
        match=(
            "Error extracting image text: Failed to extract text from image 1: "
            "model failed"
        ),
    ):
        tool._forward_impl([b"image"])


def test_tool_schema_distinguishes_transcription_from_analysis():
    assert ExtractImageTextTool.name == "extract_image_text"
    assert ExtractImageTextTool.output_type == "array"
    assert "exact text" in ExtractImageTextTool.description
    assert "image_urls_list" in ExtractImageTextTool.inputs
