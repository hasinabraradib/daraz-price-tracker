from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator


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


# ---- competitors ----


class CompetitorLinkCreate(BaseModel):
    competitor_product_id: int


class CompetitorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    competitor_product_id: int
    created_at: datetime


class CompetitorWithPrice(BaseModel):
    id: int
    competitor_product_id: int
    competitor_name: str
    competitor_daraz_url: str
    latest_price: Decimal | None = None
    currency: str | None = None
    # gap = this product's price - competitor's price; positive means
    # we're more expensive (the undercut direction)
    gap: Decimal | None = None
    gap_pct: float | None = None
    created_at: datetime


class ComparisonEntry(BaseModel):
    product_id: int
    name: str
    daraz_url: str
    latest_price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    is_cheapest: bool
    is_self: bool


class ComparisonResponse(BaseModel):
    product_id: int
    entries: list[ComparisonEntry]


# ---- alert rules & events ----


class AlertRuleCreate(BaseModel):
    rule_type: Literal["undercut", "price_below", "price_drop_pct", "back_in_stock"]
    threshold_price: Decimal | None = None
    threshold_pct: Decimal | None = None
    channel: Literal["email", "webhook"]
    destination: str

    @model_validator(mode="after")
    def _validate(self) -> "AlertRuleCreate":
        if self.rule_type == "price_below" and self.threshold_price is None:
            raise ValueError("threshold_price is required for price_below rules")
        if self.rule_type == "price_drop_pct" and self.threshold_pct is None:
            raise ValueError("threshold_pct is required for price_drop_pct rules")
        if self.rule_type == "price_drop_pct" and not (0 < self.threshold_pct <= 100):
            raise ValueError("threshold_pct must be between 0 and 100")
        if self.channel == "webhook" and not self.destination.startswith(("http://", "https://")):
            raise ValueError("webhook destination must be an http(s) URL")
        if self.channel == "email" and "@" not in self.destination:
            raise ValueError("email destination must be a valid email address")
        return self


class AlertRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    rule_type: str
    threshold_price: Decimal | None
    threshold_pct: Decimal | None
    channel: str
    destination: str
    is_active: bool
    created_at: datetime


class AlertEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_rule_id: int
    triggered_at: datetime
    resolved_at: datetime | None
    trigger_price: Decimal | None
    competitor_price: Decimal | None
    competitor_product_id: int | None
    message: str
    delivery_status: str
    delivery_error: str | None
