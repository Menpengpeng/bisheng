from typing import Optional

from pydantic import BaseModel, Field


class OpenCitationItem(BaseModel):
    item_id: str = Field(description='Citation chunk ID')
    chunk_id: Optional[str] = Field(default=None, description='Knowledge chunk ID')
    chunk_index: Optional[int] = Field(default=None, description='Chunk index in the source file')
    content: Optional[str] = Field(default=None, description='Matched source text')
    bbox: Optional[str] = Field(default=None, description='Bounding box information')
    page: Optional[int] = Field(default=None, description='Page number')


class OpenCitationResponse(BaseModel):
    file_id: Optional[int] = Field(default=None, description='Knowledge file ID')
    file_name: Optional[str] = Field(default=None, description='Knowledge file name')
    file_type: Optional[str] = Field(default=None, description='Knowledge file type')
    knowledge_name: Optional[str] = Field(default=None, description='Knowledge base name')
    download_url: Optional[str] = Field(default=None, description='File download URL')
    preview_url: Optional[str] = Field(default=None, description='File preview URL')
    bbox: Optional[str] = Field(default=None, description='Bounding box information')
    items: list[OpenCitationItem] = Field(default_factory=list, description='Matched source chunks')
