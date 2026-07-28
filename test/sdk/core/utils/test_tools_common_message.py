from sdk.nexent.core.utils.tools_common_message import SearchResultTextMessage


def _build_message(*, url, source_type):
    return SearchResultTextMessage(
        title="Example result",
        url=url,
        text="Example content",
        source_type=source_type,
        cite_index=1,
        tool_sign="exa_search",
    )


def test_to_model_dict_exposes_url_for_internet_result():
    result = _build_message(
        url=" https://example.com/source ",
        source_type="url",
    ).to_model_dict()

    assert result == {
        "title": "Example result",
        "text": "Example content",
        "index": "exa_search1",
        "url": "https://example.com/source",
    }


def test_to_model_dict_uses_none_for_missing_internet_url():
    result = _build_message(url="", source_type="url").to_model_dict()

    assert result["url"] is None


def test_to_model_dict_does_not_expose_url_for_non_internet_result():
    result = _build_message(
        url="s3://private-bucket/document.txt",
        source_type="file",
    ).to_model_dict()

    assert "url" not in result
