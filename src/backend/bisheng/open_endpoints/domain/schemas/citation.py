
from pydantic import BaseModel, Field


class OpenCitationItem(BaseModel):
    item_id: str = Field(description='Citation chunk ID')
    chunk_id: str | None = Field(default=None, description='Knowledge chunk ID')
    chunk_index: int | None = Field(default=None, description='Chunk index in the source file')
    content: str | None = Field(default=None, description='Matched source text')
    bbox: str | None = Field(default=None, description='Bounding box information')
    page: int | None = Field(default=None, description='Page number')


class OpenCitationResponse(BaseModel):
    file_id: int | None = Field(default=None, description='Knowledge file ID')
    file_name: str | None = Field(default=None, description='Knowledge file name')
    file_type: str | None = Field(default=None, description='Knowledge file type')
    knowledge_name: str | None = Field(default=None, description='Knowledge base name')
    download_url: str | None = Field(default=None, description='File download URL')
    preview_url: str | None = Field(default=None, description='File preview URL')
    bbox: str | None = Field(default=None, description='Bounding box information')
    # 二开: 返回命中的原文片段列表
    items: list[OpenCitationItem] = Field(default_factory=list, description='Matched source chunks')
