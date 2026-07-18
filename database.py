"""Database engine and session setup for PaperShelf.

SQLite lives at storage/papershelf.db. Tables are created on startup if
missing (see init_db, called from main.py's lifespan handler).
"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# All app data lives under storage/ (gitignored): the DB, original PDFs,
# converted HTML, and extracted figure images.
STORAGE_DIR = Path(__file__).resolve().parent / "storage"
PDF_DIR = STORAGE_DIR / "pdfs"
HTML_DIR = STORAGE_DIR / "html"
FIGURE_DIR = STORAGE_DIR / "figures"

DATABASE_URL = f"sqlite:///{STORAGE_DIR / 'papershelf.db'}"

# check_same_thread=False lets FastAPI background tasks (which run in a
# thread pool) share the engine. Each task still opens its own Session.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class all SQLAlchemy models inherit from."""


def init_db() -> None:
    """Create the storage directories and any missing tables."""
    for directory in (STORAGE_DIR, PDF_DIR, HTML_DIR, FIGURE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    # Import models so their tables are registered on Base.metadata
    # before create_all runs.
    import models  # noqa: F401  (import needed for side effect)

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: yield a database session, always closing it."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
