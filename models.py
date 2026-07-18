"""SQLAlchemy models for PaperShelf.

The full schema (including highlights and Zotero fields used in later
phases) is created up front so the database never needs migrating between
phases.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# Association table for the many-to-many papers <-> tags relationship.
paper_tags = Table(
    "paper_tags",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[str | None] = mapped_column(Text)  # display string, e.g. "Smith J, et al."
    journal: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    doi: Mapped[str | None] = mapped_column(String, unique=True)
    zotero_key: Mapped[str | None] = mapped_column(String)  # set by Phase 3 sync

    pdf_path: Mapped[str] = mapped_column(Text, nullable=False)  # original PDF on disk
    html_path: Mapped[str | None] = mapped_column(Text)  # converted reader HTML

    # unread | reading | read
    status: Mapped[str] = mapped_column(String, default="unread")
    # 0.0-1.0 scroll position in the reader
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # pending | processing | done | failed
    conversion_status: Mapped[str] = mapped_column(String, default="pending")
    # Human-readable reason when conversion_status == "failed"
    conversion_error: Mapped[str | None] = mapped_column(Text)

    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime)

    figures: Mapped[list["Figure"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="Figure.display_order"
    )
    highlights: Mapped[list["Highlight"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(secondary=paper_tags, back_populates="papers")


class Figure(Base):
    __tablename__ = "figures"

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)  # searchable from the library
    display_order: Mapped[int | None] = mapped_column(Integer)

    paper: Mapped[Paper] = relationship(back_populates="figures")


class Highlight(Base):
    """A saved text highlight (Phase 2). Anchored by exact text plus ~50
    chars of surrounding context so it can be re-found after HTML changes."""

    __tablename__ = "highlights"

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str | None] = mapped_column(Text)
    suffix: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str] = mapped_column(String, default="yellow")  # yellow | green | blue | pink
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    zotero_synced: Mapped[bool] = mapped_column(Boolean, default=False)

    paper: Mapped[Paper] = relationship(back_populates="highlights")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # stored lowercase

    papers: Mapped[list[Paper]] = relationship(secondary=paper_tags, back_populates="tags")


class Setting(Base):
    """Simple key/value store for secrets and config (e.g. the Zotero API
    key in Phase 3) so they never live in code or environment files."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
