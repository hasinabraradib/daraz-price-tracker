from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import desc

from shared.database import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    daraz_url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # "Option B" ownership — see api/app/deps.py's get_owner_email docstring.
    # Nullable and unvalidated: this is a free-text label read straight off
    # an X-Owner-Email request header, not tied to any authenticated
    # identity. It exists so a demo/frontend user only sees their own
    # products, not to restrict who can see or do what — anyone can type
    # any email and see/act on whatever's under it. NULL means "created
    # before this column existed, or created with no header" and is never
    # filtered out implicitly.
    owner_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    price_snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    scrape_attempts: Mapped[list["ScrapeAttempt"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index(
            "ix_price_snapshots_product_id_scraped_at",
            "product_id",
            desc("scraped_at"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    in_stock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    raw_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    product: Mapped["Product"] = relationship(back_populates="price_snapshots")


class ScrapeAttempt(Base):
    __tablename__ = "scrape_attempts"
    __table_args__ = (
        Index(
            "ix_scrape_attempts_product_id_attempted_at",
            "product_id",
            desc("attempted_at"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="scrape_attempts")


class ProductCompetitor(Base):
    """'Watch this competitor for this product of mine.' Both columns
    reference products.id; no ORM relationship() here (queried explicitly
    via select() like everywhere else in this codebase) since a
    self-referential FK pair needs relationship(foreign_keys=...)
    disambiguation for no real benefit at our current scale."""

    __tablename__ = "product_competitors"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "competitor_product_id", name="uq_product_competitor_pair"
        ),
        CheckConstraint(
            "product_id != competitor_product_id", name="ck_product_competitor_not_self"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    competitor_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('undercut', 'price_below', 'price_drop_pct', 'back_in_stock')",
            name="ck_alert_rules_rule_type",
        ),
        CheckConstraint("channel IN ('email', 'webhook')", name="ck_alert_rules_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # only meaningful (and required, enforced at the API layer) for
    # rule_type="price_below"
    threshold_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    # only meaningful (and required, enforced at the API layer) for
    # rule_type="price_drop_pct" — a percentage, e.g. 10 means "10%"
    threshold_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Same "Option B" ownership as Product.owner_email — see there, and
    # api/app/deps.py. Denormalized onto AlertRule too (rather than only
    # on Product, joining through product_id every time) so filtering
    # "my alert rules"/"my alert events" is a plain WHERE, no join needed.
    owner_email: Mapped[str | None] = mapped_column(String(320), nullable=True)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'failed')",
            name="ck_alert_events_delivery_status",
        ),
        Index(
            "ix_alert_events_alert_rule_id_triggered_at",
            "alert_rule_id",
            desc("triggered_at"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_rule_id: Mapped[int] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # NULL while the underlying condition still holds — an "open" alert.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    competitor_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    competitor_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(String(1024), nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    delivery_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
