from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, HttpUrl


class ProductCreate(BaseModel):
    name: str
    daraz_url: HttpUrl


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    daraz_url: str
    created_at: datetime
    is_active: bool


class PriceSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    price: Decimal
    currency: str
    in_stock: bool
    scraped_at: datetime
    raw_title: str | None


class LatestPrice(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price: Decimal
    currency: str
    in_stock: bool
    scraped_at: datetime


class ProductWithLatestPrice(ProductRead):
    latest_price: LatestPrice | None = None


class ScrapeQueuedResponse(BaseModel):
    queued: bool
    queue_depth: int


class QueueDepthResponse(BaseModel):
    depth: int


class ScrapeAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    attempted_at: datetime
    success: bool
    error_type: str | None
    error_message: str | None
    duration_ms: int
    attempt_number: int


class AttemptHistoryEntry(BaseModel):
    attempt_number: int
    attempted_at: datetime
    error_type: str
    error_message: str


class OriginalJob(BaseModel):
    product_id: int
    url: str
    enqueued_at: datetime


class DeadLetterRead(BaseModel):
    job_id: str
    original_job: OriginalJob
    attempts: list[AttemptHistoryEntry]
    final_error_type: str
    final_error_message: str
    dead_lettered_at: datetime


class ReplayResponse(BaseModel):
    replayed: bool
    job_id: str


class ScrapeHealthStats(BaseModel):
    total_attempts: int
    success_rate: float | None
    failures_by_error_type: dict[str, int]
    queue_depth: int
    delayed_queue_depth: int
    dead_letter_depth: int
