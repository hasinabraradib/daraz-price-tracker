from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_owned_product_or_404
from app.schemas import (
    ComparisonEntry,
    ComparisonResponse,
    CompetitorLinkCreate,
    CompetitorRead,
    CompetitorWithPrice,
)
from shared.database import get_db
from shared.models import PriceSnapshot, Product, ProductCompetitor

router = APIRouter(prefix="/products", tags=["competitors"])


async def _get_product_or_404(db: AsyncSession, product_id: int) -> Product:
    # Deliberately not owner-checked, unlike get_owned_product_or_404 —
    # this is used for the *competitor* side of a link (payload.competitor_product_id),
    # and linking someone else's tracked product as your competitor for
    # comparison purposes is intended, not a leak: it only exposes the
    # competitor's price/name, which the /comparison and /competitors
    # responses already surface by design.
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return product


async def _latest_snapshots_by_product(
    db: AsyncSession, product_ids: list[int]
) -> dict[int, PriceSnapshot]:
    if not product_ids:
        return {}
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id.in_(product_ids))
        .distinct(PriceSnapshot.product_id)
        .order_by(PriceSnapshot.product_id, PriceSnapshot.scraped_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {row.product_id: row for row in rows}


@router.post(
    "/{product_id}/competitors",
    response_model=CompetitorRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_competitor(
    product_id: int,
    payload: CompetitorLinkCreate,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    if payload.competitor_product_id == product_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a product can't compete with itself",
        )
    await _get_product_or_404(db, payload.competitor_product_id)

    link = ProductCompetitor(
        product_id=product_id, competitor_product_id=payload.competitor_product_id
    )
    db.add(link)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="this competitor is already linked"
        ) from exc
    await db.refresh(link)
    return link


@router.get("/{product_id}/competitors", response_model=list[CompetitorWithPrice])
async def list_competitors(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    links = (
        await db.execute(
            select(ProductCompetitor).where(ProductCompetitor.product_id == product_id)
        )
    ).scalars().all()

    competitor_ids = [link.competitor_product_id for link in links]
    products_by_id = {
        p.id: p
        for p in (
            await db.execute(select(Product).where(Product.id.in_(competitor_ids)))
        ).scalars().all()
    }
    snapshots = await _latest_snapshots_by_product(db, [product_id, *competitor_ids])
    this_snapshot = snapshots.get(product_id)

    results = []
    for link in links:
        competitor = products_by_id[link.competitor_product_id]
        snapshot = snapshots.get(link.competitor_product_id)

        gap: Decimal | None = None
        gap_pct: float | None = None
        if this_snapshot is not None and snapshot is not None:
            gap = this_snapshot.price - snapshot.price
            if snapshot.price != 0:
                gap_pct = float(gap / snapshot.price * 100)

        results.append(
            CompetitorWithPrice(
                id=link.id,
                competitor_product_id=link.competitor_product_id,
                competitor_name=competitor.name,
                competitor_daraz_url=competitor.daraz_url,
                latest_price=snapshot.price if snapshot else None,
                currency=snapshot.currency if snapshot else None,
                gap=gap,
                gap_pct=gap_pct,
                created_at=link.created_at,
            )
        )
    return results


@router.delete(
    "/{product_id}/competitors/{competitor_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_competitor(
    product_id: int,
    competitor_id: int,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    stmt = select(ProductCompetitor).where(
        ProductCompetitor.product_id == product_id,
        ProductCompetitor.competitor_product_id == competitor_id,
    )
    link = (await db.execute(stmt)).scalars().first()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="competitor link not found"
        )
    await db.delete(link)
    await db.commit()


@router.get("/{product_id}/comparison", response_model=ComparisonResponse)
async def comparison(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    links = (
        await db.execute(
            select(ProductCompetitor).where(ProductCompetitor.product_id == product_id)
        )
    ).scalars().all()

    all_ids = [product_id, *[link.competitor_product_id for link in links]]
    products_by_id = {
        p.id: p
        for p in (await db.execute(select(Product).where(Product.id.in_(all_ids)))).scalars().all()
    }
    snapshots = await _latest_snapshots_by_product(db, all_ids)

    cheapest_price = min(
        (snap.price for snap in snapshots.values()), default=None
    )

    entries = [
        ComparisonEntry(
            product_id=pid,
            name=products_by_id[pid].name,
            daraz_url=products_by_id[pid].daraz_url,
            latest_price=snapshots[pid].price if pid in snapshots else None,
            currency=snapshots[pid].currency if pid in snapshots else None,
            in_stock=snapshots[pid].in_stock if pid in snapshots else None,
            is_cheapest=(
                pid in snapshots
                and cheapest_price is not None
                and snapshots[pid].price == cheapest_price
            ),
            is_self=(pid == product_id),
        )
        for pid in all_ids
    ]

    return ComparisonResponse(product_id=product_id, entries=entries)
