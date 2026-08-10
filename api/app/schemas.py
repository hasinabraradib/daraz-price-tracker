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
