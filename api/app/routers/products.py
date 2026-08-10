from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_db
from shared.models import PriceSnapshot, Product
from shared.queue import enqueue_job
from shared.queue import queue_depth as get_queue_depth

from app.schemas import (
    LatestPrice,
    PriceSnapshotRead,
    ProductCreate,
    ProductRead,
    ProductWithLatestPrice,
    ScrapeQueuedResponse,
)
from app.url_utils import normalize_daraz_url

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(get_db)):
    product = Product(
        name=payload.name, daraz_url=normalize_daraz_url(str(payload.daraz_url))
    )
    db.add(product)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a product with this daraz_url already exists",
        )
    await db.refresh(product)
    return product


@router.get("", response_model=list[ProductWithLatestPrice])
async def list_products(db: AsyncSession = Depends(get_db)):
    products = (await db.execute(select(Product).order_by(Product.id))).scalars().all()

    # one row per product_id, the most recent by scraped_at
    latest_stmt = (
        select(PriceSnapshot)
        .distinct(PriceSnapshot.product_id)
        .order_by(PriceSnapshot.product_id, PriceSnapshot.scraped_at.desc())
    )
    latest_by_product = {
        snap.product_id: snap
        for snap in (await db.execute(latest_stmt)).scalars().all()
    }

    return [
        ProductWithLatestPrice(
            **ProductRead.model_validate(product).model_dump(),
            latest_price=(
                LatestPrice.model_validate(latest_by_product[product.id])
                if product.id in latest_by_product
                else None
            ),
        )
        for product in products
    ]


@router.get("/{product_id}/history", response_model=list[PriceSnapshotRead])
async def product_history(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.scraped_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.post(
    "/{product_id}/scrape",
    response_model=ScrapeQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_scrape(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    await enqueue_job(product_id=product.id, url=product.daraz_url)
    depth = await get_queue_depth()
    return ScrapeQueuedResponse(queued=True, queue_depth=depth)
