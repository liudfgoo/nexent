"""Extract visible text from images using the configured vision model."""

import logging
from io import BytesIO
from typing import List

from jinja2 import StrictUndefined, Template
from pydantic import Field
from smolagents.tools import Tool

from ...core.models import OpenAIVLModel
from ...core.utils.observer import MessageObserver
from ...core.utils.prompt_template_utils import get_prompt_template
from ...core.utils.tools_common_message import ToolCategory, ToolSign
from ...multi_modal.load_save_object import LoadSaveObjectManager
from ...storage import MinIOStorageClient

logger = logging.getLogger("extract_image_text_tool")


class ExtractImageTextTool(Tool):
    """Transcribe visible text from images without summarizing or correcting it."""

    name = "extract_image_text"
    description = (
        "Extract and transcribe all visible text from one or more images. "
        "Use this tool for screenshots, labels, serial numbers, code, tables, "
        "or any task that requires exact text rather than a general image description. "
        "The transcription preserves duplicates, capitalization, punctuation, "
        "line breaks, and reading order. Image sources may be S3, HTTP, or HTTPS URLs."
    )
    description_zh = (
        "从一张或多张图片中提取并忠实转录所有可见文字。适用于截图、标签、序列号、代码、"
        "表格以及需要原文而非图片概述的任务。转录会保留重复项、大小写、标点、换行和阅读顺序。"
        "图片支持 S3、HTTP 和 HTTPS URL。"
    )

    inputs = {
        "image_urls_list": {
            "type": "array",
            "description": (
                "List of image URLs to transcribe. Supports s3://bucket/key, "
                "/bucket/key, http://, and https:// URLs."
            ),
            "description_zh": (
                "需要转录的图片 URL 列表。支持 s3://bucket/key、/bucket/key、"
                "http:// 和 https:// URL。"
            ),
        }
    }

    init_param_descriptions = {
        "observer": {"description": "Message observer"},
        "vlm_model": {"description": "The vision model used for transcription"},
        "selected_model_id": {
            "description": (
                "Optional Nexent image understanding model ID. If omitted, "
                "the default image understanding model is used."
            )
        },
        "storage_client": {"description": "Storage client for downloading images"},
        "validate_url_access": {
            "description": "Callback function used to validate URL access"
        },
    }

    output_type = "array"
    category = ToolCategory.MULTIMODAL.value
    tool_sign = ToolSign.MULTIMODAL_OPERATION.value

    def __init__(
        self,
        observer: MessageObserver = Field(
            description="Message observer", default=None, exclude=True
        ),
        vlm_model: OpenAIVLModel = Field(
            description="The vision model used for transcription",
            default=None,
            exclude=True,
        ),
        selected_model_id: int = Field(
            description="Optional image understanding model ID", default=None
        ),
        storage_client: MinIOStorageClient = Field(
            description="Storage client for downloading images",
            default=None,
            exclude=True,
        ),
        validate_url_access: callable = Field(
            description="Callback function used to validate URL access",
            default=None,
            exclude=True,
        ),
    ):
        super().__init__()
        self.observer = observer
        self.vlm_model = vlm_model
        self.selected_model_id = selected_model_id
        self.storage_client = storage_client
        self._is_chinese = bool(observer and observer.lang == "zh")

        validate_callback = (
            validate_url_access
            if validate_url_access is not None and callable(validate_url_access)
            else None
        )
        self.mm = LoadSaveObjectManager(
            storage_client=self.storage_client,
            validate_url_access=validate_callback,
        )
        self.forward = self.mm.load_object(
            input_names=["image_urls_list"]
        )(self._forward_impl)

    def _forward_impl(self, image_urls_list: List[bytes]) -> List[str]:
        """Return one strict transcription for each input image."""
        if self.vlm_model is None:
            error_msg = (
                "图片理解模型未配置，请联系管理员配置图片理解模型后重试"
                if self._is_chinese
                else (
                    "Image understanding model is not configured. Please contact "
                    "your administrator to configure it and try again."
                )
            )
            logger.error(error_msg)
            raise Exception(error_msg)

        if image_urls_list is None:
            raise ValueError("image_urls cannot be None")
        if not isinstance(image_urls_list, list):
            raise ValueError("image_urls must be a list of bytes")
        if not image_urls_list:
            raise ValueError("image_urls must contain at least one image")

        language = self.observer.lang if self.observer else "en"
        prompts = get_prompt_template(
            template_type="extract_image_text", language=language
        )
        system_prompt = Template(
            prompts["system_prompt"], undefined=StrictUndefined
        ).render()

        try:
            transcriptions: List[str] = []
            for index, image_bytes in enumerate(image_urls_list, start=1):
                logger.info("Transcribing visible text from image #%s", index)
                try:
                    response = self.vlm_model.analyze_image(
                        image_input=BytesIO(image_bytes),
                        system_prompt=system_prompt,
                        temperature=0,
                    )
                except Exception as exc:
                    error_msg = (
                        f"图片{index}文字提取失败: {exc}。请检查图片理解模型配置是否正确。"
                        if self._is_chinese
                        else (
                            f"Failed to extract text from image {index}: {exc}. "
                            "Please check if the image understanding model is "
                            "configured correctly."
                        )
                    )
                    raise Exception(error_msg) from exc
                transcriptions.append(response.content)
            return transcriptions
        except Exception as exc:
            logger.error("Error extracting image text: %s", exc, exc_info=True)
            raise Exception(f"Error extracting image text: {exc}") from exc
