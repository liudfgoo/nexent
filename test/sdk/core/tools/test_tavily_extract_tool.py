import json
from unittest.mock import MagicMock, patch

from sdk.nexent.core.tools.tavily_extract_tool import TavilyExtractTool


def test_tavily_extract_returns_complete_response():
    response = {
        "results": [
            {
                "url": "https://example.com/source",
                "raw_content": "# Complete source\n\nEvidence",
            }
        ],
        "failed_results": [],
    }

    with patch("sdk.nexent.core.tools.tavily_extract_tool.TavilyClient") as client_class:
        client = MagicMock()
        client.extract.return_value = response
        client_class.return_value = client

        tool = TavilyExtractTool(
            tavily_api_key="test-key",
            extract_depth="advanced",
            content_format="markdown",
        )
        result = json.loads(tool.forward("https://example.com/source"))

    client.extract.assert_called_once_with(
        urls="https://example.com/source",
        extract_depth="advanced",
        format="markdown",
    )
    assert result == response
