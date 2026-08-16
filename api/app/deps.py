from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_db
from shared.models import Product


async def get_owner_email(x_owner_email: str | None = Header(default=None)) -> str | None:
    """Reads the optional X-Owner-Email request header.

    THIS IS NOT AUTHENTICATION. Nothing here verifies that whoever sends
    this header actually controls that email address — there's no
    password, no token, no proof of identity. Anyone can type any email
    and see or act on whatever was created under it. It exists purely so
    the demo frontend (web/) can show "your" products without building a
    real login flow: the frontend stores an email in localStorage and
    sends it back on every request, and every dependency/query below
    reads it. When the header is absent, nothing gets filtered — every
    product/alert rule is visible, matching pre-ownership behavior
    exactly. Real auth (sessions, JWTs, a verified identity provider) is
    listed as future work in the main README.
    """
    return x_owner_email


async def get_owned_product_or_404(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    owner_email: str | None = Depends(get_owner_email),
) -> Product:
    """Fetches a product by id, 404ing if it doesn't exist OR if an
    X-Owner-Email header was sent and doesn't match the product's
    recorded owner. A product with no recorded owner (owner_email IS
    NULL — created before this column existed, or created with no
    header) is never hidden by this check, same "absent = no filtering"
    rule as everywhere else this project applies it. 404 rather than 403
    on a mismatch so this doesn't function as an existence oracle for
    product IDs that aren't yours.
    """
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    if (
        owner_email is not None
        and product.owner_email is not None
        and product.owner_email != owner_email
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return product
