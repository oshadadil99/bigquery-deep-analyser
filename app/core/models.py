from pydantic import BaseModel,Field
from typing import Any, Dict, Optional, List, Literal, Union
from datetime import datetime

class AskRequest(BaseModel):
    shandover: Dict[str, Any]          
    encrypt: bool = Field(default=True)

class ImageUrl(BaseModel):
    url: str
    format: Optional[str] = None

class SourceDocument(BaseModel):
    chunk_index: Optional[int] = None
    mime_type: Optional[str] = None
    model: Optional[str] = None
    score: Optional[float] = None
    file_id: Optional[str] = None
    filename: Optional[str] = None
    store_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    type: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[ImageUrl] = None
    ocr_text: Optional[str] = None
    summary: Optional[str] = None
    
    class Config:
        extra = "allow"  # Allow additional fields

class SearchResponse(BaseModel):
    object: str = "list"
    data: List[SourceDocument] = Field(default_factory=list)
    
    class Config:
        extra = "allow"

class AskResponse(BaseModel):
    answer: str
    sources: List[SourceDocument] = Field(default_factory=list)


#---------------------------------------------------------------------Chanupa Dev---------------------------------------------------------------
#-----------------------------------------------------------------------------------------------------------------------------------------------

class ExpiresAfter(BaseModel):
    anchor: Literal["last_active_at"] = "last_active_at"
    days: int = Field(..., ge=1)


class StoreConfig(BaseModel):
    contextualization: Union[bool, Dict[str, Any]] = False


class CreateStoreRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    is_public: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

    config: Optional[StoreConfig] = None

    expires_after: Optional[ExpiresAfter] = None
    file_ids: Optional[List[str]] = None


class FileCounts(BaseModel):
    in_progress: int
    completed: int
    failed: int
    cancelled: int
    pending: int
    total: int


class CreateStoreResponse(BaseModel):
    id: str
    object: Literal["store"]

    name: str
    description: Optional[str] = None
    is_public: bool
    metadata: Dict[str, Any] = Field(default_factory=dict)

    config: Dict[str, Any] = Field(default_factory=dict)
    file_counts: FileCounts

    status: Literal["expired", "in_progress", "completed"]
    usage_bytes: int

    expires_after: Optional[Any] = None
    expires_at: Optional[datetime] = None

    created_at: datetime
    updated_at: datetime
    last_active_at: Optional[datetime] = None


class Pagination(BaseModel):
    first_cursor: Optional[str] = None
    last_cursor: Optional[str] = None
    has_more: bool = False
    total: Optional[int] = None  

class ListStoresResponse(BaseModel):
    object: Literal["list"] = "list"
    data: List["CreateStoreResponse"]
    pagination: Pagination


class UpdateStoreRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9.-]+$")
    description: Optional[str] = Field(default=None, max_length=2000)
    is_public: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None
    expires_after: Optional["ExpiresAfter"] = None


class DeleteStoreResponse(BaseModel):
    id: str
    deleted: bool
    object: Literal["store"] = "store"



class StoreFile(BaseModel):
    id: str
    object: Literal["store.file"] = "store.file"
    filename: str

    status: str  
    last_error: Optional[Any] = None

    store_id: str
    usage_bytes: int
    version: int
    created_at: datetime

    metadata: Dict[str, Any] = Field(default_factory=dict)


class ListStoreFilesResponse(BaseModel):
    object: Literal["list"] = "list"
    data: List[StoreFile] = Field(default_factory=list)
    pagination: Pagination  


class AddStoreFileRequest(BaseModel):
    file_id: str = Field(..., min_length=1)  # uuid string

    external_id: Optional[str] = None
    overwrite: bool = True

    metadata: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    experimental: Optional[Dict[str, Any]] = None

class DeleteStoreFileResponse(BaseModel):
    id: str
    deleted: bool
    object: Literal["store.file"] = "store.file"


class StoreFileStatusResponse(BaseModel):
    file_id: str
    status: str
