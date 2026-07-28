import json

from pydantic import Field
from smolagents.tools import Tool
from tavily import TavilyClient

from ..utils.tools_common_message import ToolCategory


class TavilyExtractTool(Tool):
    name = "tavily_extract"
    description = (
        "Fetch and extract the complete readable content from a known URL. "
        "Use this after a search tool has identified a relevant source URL; "
        "do not use it for broad web discovery."
    )
    description_zh = (
        "从已知 URL 获取并提取完整的可读正文。应在搜索工具定位到相关来源 URL 后使用；"
        "不要用它进行宽泛的互联网搜索。"
    )
    inputs = {
        "url": {
            "type": "string",
            "description": "The exact public webpage URL whose content should be extracted.",
            "description_zh": "需要提取正文的确切公开网页 URL。",
        }
    }
    init_param_descriptions = {
        "tavily_api_key": {
            "description": "Tavily API key",
            "description_zh": "Tavily API 密钥",
        },
        "extract_depth": {
            "description": "Extraction depth: basic or advanced",
            "description_zh": "提取深度：basic 或 advanced",
        },
        "content_format": {
            "description": "Extracted content format: markdown or text",
            "description_zh": "提取内容格式：markdown 或 text",
        },
    }
    output_type = "string"
    category = ToolCategory.SEARCH.value

    def __init__(
        self,
        tavily_api_key: str = Field(description="Tavily API key"),
        extract_depth: str = Field(description="Extraction depth", default="advanced"),
        content_format: str = Field(description="Content format", default="markdown"),
    ):
        super().__init__()
        self.tavily = TavilyClient(api_key=tavily_api_key)
        self.extract_depth = extract_depth
        self.content_format = content_format

    def forward(self, url: str) -> str:
        result = self.tavily.extract(
            urls=url,
            extract_depth=self.extract_depth,
            format=self.content_format,
        )
        return json.dumps(result, ensure_ascii=False)
