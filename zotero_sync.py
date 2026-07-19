"""Zotero pull sync (Phase 3).

Flow (driven from the Settings page):
  1. The user saves their Zotero API key + library ID (settings table —
     never in code or env files).
  2. The page lists the library's collections; the user picks which to sync.
  3. sync_collections() runs as a background task: for every journal
     article in the chosen collections that has an attached PDF, it
     downloads the PDF, imports metadata straight from Zotero (no
     Crossref needed — Zotero already has it), and runs the normal
     Marker conversion pipeline.

Dedupe rules, in order:
  - an item whose zotero_key is already in the library is skipped;
  - an item whose DOI matches an existing paper just links that paper to
    Zotero (sets its zotero_key) instead of importing a duplicate.

Progress is reported through the module-level `sync_state` dict, which
/api/zotero/status exposes to the settings page. A single global is fine
here because PaperShelf is single-user by design.

Pushing highlights back to Zotero is planned for a later version; the
API endpoint exists as a stub (see main.py).
"""

import re

from convert import convert_paper
from database import PDF_DIR, SessionLocal
from models import Paper, Setting
from sqlalchemy.orm import Session

# Live progress for the settings page. Reset at the start of every sync.
sync_state: dict = {
    "running": False,
    "message": "No sync has run yet.",
    "imported": 0,   # new papers created
    "linked": 0,     # existing papers linked by DOI
    "skipped": 0,    # already synced / no PDF / not an article
    "failed": 0,     # per-item errors (sync continues past them)
    "current": "",   # title currently being processed
}


# --------------------------------------------------------------------------
# Settings helpers (shared with main.py)
# --------------------------------------------------------------------------

def get_setting(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else None


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


# --------------------------------------------------------------------------
# Zotero client
# --------------------------------------------------------------------------

def _make_client(api_key: str, library_id: str):
    """Build the pyzotero client. Kept tiny and separate so tests can
    substitute a fake, and so pyzotero is only imported when needed."""
    from pyzotero import zotero

    return zotero.Zotero(library_id, "user", api_key)


def get_client(db: Session):
    """Client from saved settings, or None if credentials are missing."""
    api_key = get_setting(db, "zotero_api_key")
    library_id = get_setting(db, "zotero_library_id")
    if not api_key or not library_id:
        return None
    return _make_client(api_key, library_id)


def list_collections(zot) -> list[dict]:
    """All collections in the library as [{key, name, count}]."""
    collections = zot.everything(zot.collections())
    result = [
        {
            "key": c["data"]["key"],
            "name": c["data"]["name"],
            "count": c.get("meta", {}).get("numItems", 0),
        }
        for c in collections
    ]
    result.sort(key=lambda c: c["name"].lower())
    return result


# --------------------------------------------------------------------------
# Metadata helpers
# --------------------------------------------------------------------------

def _year_from(date_str: str | None) -> int | None:
    """Zotero dates are free-form ("2015-10-08", "October 2015", …) —
    pull out the first plausible 4-digit year."""
    if not date_str:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", date_str)
    return int(match.group(0)) if match else None


def _authors_from(creators: list[dict] | None) -> str | None:
    """Compact display string ("Furuta G, et al."), matching the style
    metadata.py produces from Crossref."""
    names = []
    for creator in creators or []:
        if creator.get("creatorType") not in (None, "author"):
            continue
        family = creator.get("lastName") or creator.get("name")
        if not family:
            continue
        initial = (creator.get("firstName") or "")[:1]
        names.append(f"{family} {initial}".strip())
    if not names:
        return None
    if len(names) > 2:
        return f"{names[0]}, et al."
    return ", ".join(names)


def _find_pdf_attachment(zot, item_key: str) -> dict | None:
    """First importable PDF attachment of an item, or None.

    Note: only attachments actually stored in Zotero ("imported" link
    modes) can be downloaded through the API. A "linked file" that lives
    on some other computer's disk cannot — those items are skipped.
    """
    for child in zot.children(item_key):
        data = child.get("data", {})
        if (
            data.get("itemType") == "attachment"
            and data.get("contentType") == "application/pdf"
            and data.get("linkMode", "").startswith("imported")
        ):
            return child
    return None


# --------------------------------------------------------------------------
# The sync task
# --------------------------------------------------------------------------

def sync_collections(collection_keys: list[str]) -> None:
    """Background task: import every syncable item in the given collections.

    Items are processed one at a time, and each imported paper is
    converted before moving on — Marker is memory-hungry, so serial
    processing is deliberate.
    """
    sync_state.update(
        running=True, message="Starting sync…",
        imported=0, linked=0, skipped=0, failed=0, current="",
    )

    db = SessionLocal()
    try:
        zot = get_client(db)
        if zot is None:
            sync_state.update(running=False, message="Zotero credentials are not configured.")
            return

        for key in collection_keys:
            try:
                # Top-level items only (attachments/notes come via children);
                # the itemType filter keeps standalone attachments out.
                items = zot.everything(zot.collection_items_top(key, itemType="-attachment"))
            except Exception as exc:  # noqa: BLE001 - bad key / network / auth
                sync_state["failed"] += 1
                sync_state["message"] = f"Couldn't read collection {key}: {exc}"
                continue

            for item in items:
                try:
                    _import_item(db, zot, item)
                except Exception:  # noqa: BLE001 - keep going past one bad item
                    db.rollback()
                    sync_state["failed"] += 1

        sync_state.update(
            running=False, current="",
            message=(
                f"Sync finished: {sync_state['imported']} imported, "
                f"{sync_state['linked']} linked, {sync_state['skipped']} skipped, "
                f"{sync_state['failed']} failed."
            ),
        )
    finally:
        db.close()


def _import_item(db: Session, zot, item: dict) -> None:
    """Import one Zotero item, respecting the dedupe rules."""
    data = item.get("data", {})
    zotero_key = data.get("key")
    title = data.get("title") or "Untitled"

    # Notes, standalone attachments, etc. are not papers.
    if data.get("itemType") in ("note", "attachment"):
        sync_state["skipped"] += 1
        return

    sync_state["current"] = title

    # Dedupe 1: already imported from Zotero.
    if db.query(Paper).filter(Paper.zotero_key == zotero_key).first():
        sync_state["skipped"] += 1
        return

    # Dedupe 2: same DOI already in the library (e.g. uploaded by hand
    # before Zotero sync existed) — link it rather than duplicate it.
    doi = (data.get("DOI") or "").strip() or None
    if doi:
        existing = db.query(Paper).filter(Paper.doi == doi).first()
        if existing:
            existing.zotero_key = zotero_key
            db.commit()
            sync_state["linked"] += 1
            return

    attachment = _find_pdf_attachment(zot, zotero_key)
    if attachment is None:
        sync_state["skipped"] += 1  # nothing to read without a PDF
        return

    # Download under a name derived from the Zotero key: unique, and
    # immune to whatever the original filename contains.
    filename = f"zotero_{zotero_key}.pdf"
    zot.dump(attachment["data"]["key"], filename, str(PDF_DIR))

    paper = Paper(
        title=title,
        authors=_authors_from(data.get("creators")),
        journal=data.get("publicationTitle") or None,
        year=_year_from(data.get("date")),
        doi=doi,
        zotero_key=zotero_key,
        pdf_path=str(PDF_DIR / filename),
    )
    db.add(paper)
    db.commit()
    db.refresh(paper)
    sync_state["imported"] += 1

    # Convert now, serially (see sync_collections docstring). Conversion
    # failures are recorded on the paper row and retryable from the
    # library — they don't fail the sync.
    convert_paper(paper.id)
