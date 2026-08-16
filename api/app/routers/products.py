from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_owned_product_or_404, get_owner_email
from app.schemas import (
    LatestPrice,
    PriceSnapshotRead,
    ProductCreate,
    ProductRead,
    ProductWithLatestPrice,
    ScrapeAttemptRead,
    ScrapeQueuedResponse,
)
from app.url_utils import InvalidProductUrlError, normalize_daraz_url
from shared.database import get_db
from shared.models import PriceSnapshot, Product, ScrapeAttempt
from shared.queue import enqueue_job
from shared.queue import queue_depth as get_queue_depth

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    db: AsyncSession = Depends(get_db),
    owner_email: str | None = Depends(get_owner_email),
):
    try:
        daraz_url = normalize_daraz_url(str(payload.daraz_url))
    except InvalidProductUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    product = Product(name=payload.name, daraz_url=daraz_url, owner_email=owner_email)
    db.add(product)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a product with this daraz_url already exists",
        ) from exc
    await db.refresh(product)
    return product


@router.get("", response_model=list[ProductWithLatestPrice])
async def list_products(
    daraz_url: str | None = None,
    db: AsyncSession = Depends(get_db),
    owner_email: str | None = Depends(get_owner_email),
):
    stmt = select(Product).order_by(Product.id)

    if daraz_url is not None:
        # Exact-match lookup, not a listing operation — used by web/'s
        # "link a competitor by URL" flow to check whether a product for
        # that URL already exists before creating a duplicate (there's no
        # other way to go from a URL to a product id; POST /products
        # itself just 409s on a duplicate daraz_url without saying what
        # the existing id is). Deliberately bypasses owner_email
        # filtering below — a competitor you want to link is frequently
        # not "yours," and hiding a real match here would just cause the
        # frontend to attempt a create that 409s right after. Invalid
        # URLs quietly resolve to "not found" rather than a 422, so
        # callers can treat this purely as a existence check and fall
        # back to creation without special-casing malformed input twice.
        try:
            normalized = normalize_daraz_url(daraz_url)
        except InvalidProductUrlError:
            return []
        stmt = stmt.where(Product.daraz_url == normalized)
    elif owner_email is not None:
        # No header = no filtering (see app/deps.py). With a header, show
        # this owner's products plus anything with no recorded owner —
        # never hide pre-ownership/legacy rows just because someone
        # eventually typed an email in.
        stmt = stmt.where((Product.owner_email == owner_email) | (Product.owner_email.is_(None)))
    products = (await db.execute(stmt)).scalars().all()

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
async def product_history(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    product: Product = Depends(get_owned_product_or_404),
):
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.scraped_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.get("/{product_id}/attempts", response_model=list[ScrapeAttemptRead])
async def product_attempts(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    product: Product = Depends(get_owned_product_or_404),
):
    stmt = (
        select(ScrapeAttempt)
        .where(ScrapeAttempt.product_id == product_id)
        .order_by(ScrapeAttempt.attempted_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.post(
    "/{product_id}/scrape",
    response_model=ScrapeQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_scrape(
    product_id: int,
    product: Product = Depends(get_owned_product_or_404),
):
    await enqueue_job(product_id=product.id, url=product.daraz_url)
    depth = await get_queue_depth()
    return ScrapeQueuedResponse(queued=True, queue_depth=depth)
