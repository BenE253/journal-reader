"""PaperShelf — FastAPI app and routes.

Run with:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

import metadata as meta
import zotero_sync
from convert import convert_paper
from database import FIGURE_DIR, PDF_DIR, get_db, init_db, SessionLocal
from models import Figure, Highlight, Paper, Tag

HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create storage dirs/tables and recover interrupted jobs."""
    init_db()
    _reset_stuck_conversions()
    yield


def _reset_stuck_conversions() -> None:
    """Papers left in 'processing' by a crashed/restarted server would spin
    forever in the UI — mark them failed so the retry button appears."""
    db = SessionLocal()
    try:
        stuck = db.query(Paper).filter(Paper.conversion_status == "processing").all()
        for paper in stuck:
            paper.conversion_status = "failed"
            paper.conversion_error = "Server restarted during conversion. Tap retry."
        db.commit()
    finally:
        db.close()


app = FastAPI(title="PaperShelf", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# Figures are stored outside static/ (they're user data), served read-only here.
FIGURE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/figures", StaticFiles(directory=FIGURE_DIR), name="figures")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def library(
    request: Request,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    """Card list of all papers, newest first, with filters and search."""
    query = db.query(Paper)

    if status in ("unread", "reading", "read"):
        query = query.filter(Paper.status == status)

    if tag:
        query = query.join(Paper.tags).filter(Tag.name == tag.lower())

    if q:
        like = f"%{q}%"
        # Papers whose figures' captions match the search term.
        caption_paper_ids = (
            db.query(Figure.paper_id).filter(Figure.caption.ilike(like)).subquery()
        )
        query = query.filter(
            or_(
                Paper.title.ilike(like),
                Paper.authors.ilike(like),
                Paper.journal.ilike(like),
                Paper.id.in_(caption_paper_ids),
            )
        )

    papers = query.order_by(Paper.added_at.desc()).all()
    all_tags = db.query(Tag).order_by(Tag.name).all()

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "papers": papers,
            "all_tags": all_tags,
            "active_status": status,
            "active_tag": tag,
            "search_query": q or "",
        },
    )


@app.get("/api/conversion-status")
def conversion_status(db: Session = Depends(get_db)):
    """Polled by the library page while any paper is converting."""
    rows = db.query(Paper.id, Paper.conversion_status).all()
    return {str(paper_id): status for paper_id, status in rows}


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.post("/upload")
def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Save the PDF, create the paper row, fetch metadata, queue conversion."""
    filename = Path(file.filename or "paper.pdf").name  # strip any path parts
    if not filename.lower().endswith(".pdf"):
        return templates_error("Only PDF files are supported.")

    # Avoid overwriting an existing file with the same name.
    pdf_path = PDF_DIR / filename
    counter = 1
    while pdf_path.exists():
        pdf_path = PDF_DIR / f"{Path(filename).stem}_{counter}.pdf"
        counter += 1

    with pdf_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    paper = create_paper_from_pdf(db, pdf_path)
    background_tasks.add_task(convert_paper, paper.id)
    return RedirectResponse(url="/", status_code=303)


def create_paper_from_pdf(db: Session, pdf_path: Path) -> Paper:
    """Shared by the upload route and seed.py: metadata lookup + DB row.

    DOI/Crossref failures are expected (scans, supplements, older PDFs) —
    we fall back to the filename and let Marker/the user improve it later.
    """
    doi = meta.extract_doi(str(pdf_path))
    crossref = meta.fetch_crossref(doi) if doi else None

    # A DOI already in the library means a duplicate upload; reuse the row
    # and just re-run conversion rather than creating a second copy.
    if doi:
        existing = db.query(Paper).filter(Paper.doi == doi).first()
        if existing:
            pdf_path.unlink(missing_ok=True)  # discard the duplicate file
            return existing

    if crossref and crossref.title:
        paper = Paper(
            title=crossref.title,
            authors=crossref.authors,
            journal=crossref.journal,
            year=crossref.year,
            doi=doi,
            pdf_path=str(pdf_path),
        )
    else:
        paper = Paper(
            title=meta.title_from_filename(pdf_path.name),
            doi=doi,
            pdf_path=str(pdf_path),
        )

    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


def templates_error(message: str) -> HTMLResponse:
    """Minimal error page (kept simple; rarely seen in single-user use)."""
    return HTMLResponse(f"<p>{message}</p><p><a href='/'>Back to library</a></p>", status_code=400)


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

@app.get("/paper/{paper_id}", response_class=HTMLResponse)
def reader(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        return RedirectResponse(url="/")

    content = None
    if paper.conversion_status == "done" and paper.html_path:
        html_file = Path(paper.html_path)
        if html_file.exists():
            content = html_file.read_text(encoding="utf-8")

    paper.last_opened_at = datetime.utcnow()
    db.commit()

    return templates.TemplateResponse(
        request, "reader.html", {"paper": paper, "content": content}
    )


class ProgressUpdate(BaseModel):
    progress: float  # 0.0-1.0


@app.post("/paper/{paper_id}/progress")
def save_progress(paper_id: int, update: ProgressUpdate, db: Session = Depends(get_db)):
    """Debounced scroll-position save from reader.js."""
    paper = db.get(Paper, paper_id)
    if paper is None:
        return JSONResponse({"ok": False}, status_code=404)

    paper.progress = max(0.0, min(1.0, update.progress))
    # First real scroll flips an unread paper to "reading"; marking "read"
    # stays a deliberate user action in the overflow menu.
    if paper.status == "unread" and paper.progress > 0.02:
        paper.status = "reading"
    db.commit()
    return {"ok": True}


@app.get("/paper/{paper_id}/pdf")
def original_pdf(paper_id: int, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None or not Path(paper.pdf_path).exists():
        return RedirectResponse(url="/")
    return FileResponse(paper.pdf_path, media_type="application/pdf")


@app.post("/paper/{paper_id}/retry")
def retry_conversion(
    paper_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    paper = db.get(Paper, paper_id)
    if paper is not None and paper.conversion_status in ("failed", "pending"):
        paper.conversion_status = "pending"
        db.commit()
        background_tasks.add_task(convert_paper, paper.id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/paper/{paper_id}/reconvert")
def reconvert(
    paper_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """Re-run conversion on an already-converted paper (e.g. to pick up
    improved text cleanup). Highlights survive: they re-anchor by text on
    the reader, so as long as the wording is stable they reattach."""
    paper = db.get(Paper, paper_id)
    if paper is not None and paper.conversion_status != "processing":
        paper.conversion_status = "pending"
        db.commit()
        background_tasks.add_task(convert_paper, paper.id)
    return RedirectResponse(url=f"/paper/{paper_id}", status_code=303)


@app.post("/paper/{paper_id}/status")
def set_status(paper_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    """Manual status change from the reader overflow menu / edit page."""
    paper = db.get(Paper, paper_id)
    if paper is not None and status in ("unread", "reading", "read"):
        paper.status = status
        db.commit()
    return RedirectResponse(url=f"/paper/{paper_id}", status_code=303)


# --------------------------------------------------------------------------
# Highlights (Phase 2)
# --------------------------------------------------------------------------
# JSON API used by highlights.js in the reader, plus two HTML views:
# per-paper (/paper/{id}/highlights) and global (/highlights?tag=...).


class HighlightCreate(BaseModel):
    """Sent by the reader when the user highlights a selection.

    prefix/suffix are ~50 chars of surrounding context captured at creation
    time, used to re-find ("anchor") the highlight on future page loads.
    """

    text: str
    prefix: str = ""
    suffix: str = ""
    color: str = "yellow"
    note: str | None = None


class HighlightUpdate(BaseModel):
    """Partial update: only the fields present are changed."""

    color: str | None = None
    note: str | None = None


def _highlight_json(hl: Highlight) -> dict:
    """Shape a Highlight row for the reader's JavaScript."""
    return {
        "id": hl.id,
        "text": hl.text,
        "prefix": hl.prefix or "",
        "suffix": hl.suffix or "",
        "color": hl.color,
        "note": hl.note,
    }


@app.get("/api/paper/{paper_id}/highlights")
def list_highlights(paper_id: int, db: Session = Depends(get_db)):
    highlights = (
        db.query(Highlight)
        .filter(Highlight.paper_id == paper_id)
        .order_by(Highlight.created_at)
        .all()
    )
    return [_highlight_json(hl) for hl in highlights]


@app.post("/api/paper/{paper_id}/highlights")
def create_highlight(
    paper_id: int, payload: HighlightCreate, db: Session = Depends(get_db)
):
    paper = db.get(Paper, paper_id)
    if paper is None:
        return JSONResponse({"error": "paper not found"}, status_code=404)
    if not payload.text.strip():
        return JSONResponse({"error": "empty highlight"}, status_code=400)

    hl = Highlight(
        paper_id=paper_id,
        text=payload.text,
        prefix=payload.prefix[-50:],  # enforce the ~50-char context budget
        suffix=payload.suffix[:50],
        color=payload.color if payload.color in HIGHLIGHT_COLORS else "yellow",
        note=payload.note,
    )
    db.add(hl)
    db.commit()
    db.refresh(hl)
    return _highlight_json(hl)


@app.patch("/api/highlight/{highlight_id}")
def update_highlight(
    highlight_id: int, payload: HighlightUpdate, db: Session = Depends(get_db)
):
    hl = db.get(Highlight, highlight_id)
    if hl is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    if payload.color is not None and payload.color in HIGHLIGHT_COLORS:
        hl.color = payload.color
    if payload.note is not None:
        # An empty string clears the note.
        hl.note = payload.note.strip() or None
    db.commit()
    return _highlight_json(hl)


@app.delete("/api/highlight/{highlight_id}")
def delete_highlight_api(highlight_id: int, db: Session = Depends(get_db)):
    hl = db.get(Highlight, highlight_id)
    if hl is not None:
        db.delete(hl)
        db.commit()
    return {"ok": True}


@app.post("/highlight/{highlight_id}/delete")
def delete_highlight_form(
    highlight_id: int, next: str = Form("/highlights"), db: Session = Depends(get_db)
):
    """Form-post variant used by the highlights list pages (no JS needed).

    `next` is where to send the user back to; only same-site paths are
    accepted so the redirect can't point off-site.
    """
    hl = db.get(Highlight, highlight_id)
    if hl is not None:
        db.delete(hl)
        db.commit()
    if not next.startswith("/"):
        next = "/highlights"
    return RedirectResponse(url=next, status_code=303)


@app.get("/paper/{paper_id}/highlights", response_class=HTMLResponse)
def paper_highlights(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """All highlights for one paper."""
    paper = db.get(Paper, paper_id)
    if paper is None:
        return RedirectResponse(url="/")
    highlights = sorted(paper.highlights, key=lambda hl: hl.created_at)
    return templates.TemplateResponse(
        request,
        "highlights.html",
        {
            "groups": [{"paper": paper, "highlights": highlights}] if highlights else [],
            "single_paper": paper,
            "all_tags": [],
            "active_tag": None,
        },
    )


@app.get("/highlights", response_class=HTMLResponse)
def global_highlights(
    request: Request, tag: str | None = None, db: Session = Depends(get_db)
):
    """Every highlight across the library, grouped by paper, tag-filterable."""
    query = db.query(Highlight).join(Paper)
    if tag:
        query = query.join(Paper.tags).filter(Tag.name == tag.lower())
    highlights = query.order_by(Paper.added_at.desc(), Highlight.created_at).all()

    # Group by paper, preserving the newest-paper-first ordering.
    groups: list[dict] = []
    for hl in highlights:
        if not groups or groups[-1]["paper"].id != hl.paper_id:
            groups.append({"paper": hl.paper, "highlights": []})
        groups[-1]["highlights"].append(hl)

    all_tags = db.query(Tag).order_by(Tag.name).all()
    return templates.TemplateResponse(
        request,
        "highlights.html",
        {
            "groups": groups,
            "single_paper": None,
            "all_tags": all_tags,
            "active_tag": tag,
        },
    )


# --------------------------------------------------------------------------
# Settings & Zotero sync (Phase 3)
# --------------------------------------------------------------------------

DEFAULT_ZOTERO_LIBRARY_ID = "11048113"


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "api_key_set": bool(zotero_sync.get_setting(db, "zotero_api_key")),
            "library_id": zotero_sync.get_setting(db, "zotero_library_id")
                          or DEFAULT_ZOTERO_LIBRARY_ID,
            "sync_state": zotero_sync.sync_state,
        },
    )


@app.post("/settings/zotero")
def save_zotero_settings(
    api_key: str = Form(""),
    library_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save credentials. A blank API key field keeps the stored one, so
    the saved key never needs to be redisplayed in the form."""
    if api_key.strip():
        zotero_sync.set_setting(db, "zotero_api_key", api_key.strip())
    if library_id.strip():
        zotero_sync.set_setting(db, "zotero_library_id", library_id.strip())
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/api/zotero/collections")
def zotero_collections(db: Session = Depends(get_db)):
    # Client creation can itself fail (pyzotero not installed, malformed
    # library ID), so it sits inside the try as well — the page should
    # always get a readable message, never a bare 500.
    try:
        zot = zotero_sync.get_client(db)
        if zot is None:
            return JSONResponse(
                {"error": "Zotero credentials are not configured."}, status_code=400
            )
        return zotero_sync.list_collections(zot)
    except ModuleNotFoundError:
        return JSONResponse(
            {"error": "The pyzotero package is not installed. "
                      "Run: pip install -r requirements.txt (inside the venv), "
                      "then restart the server."},
            status_code=500,
        )
    except Exception as exc:  # noqa: BLE001 - bad key, network, Zotero down
        return JSONResponse(
            {"error": f"Couldn't reach Zotero: {type(exc).__name__}: {exc}"},
            status_code=502,
        )


@app.post("/zotero/sync")
def start_zotero_sync(
    background_tasks: BackgroundTasks,
    collections: list[str] = Form(...),
):
    """Kick off the pull sync for the chosen collections."""
    if not zotero_sync.sync_state["running"]:
        background_tasks.add_task(zotero_sync.sync_collections, collections)
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/api/zotero/status")
def zotero_status():
    return zotero_sync.sync_state


@app.post("/api/zotero/push-highlights")
def push_highlights_stub():
    """Planned v2 feature: write highlights to Zotero as a child note.
    Stubbed so the API shape is reserved."""
    return JSONResponse(
        {"detail": "Pushing highlights to Zotero is not implemented yet."},
        status_code=501,
    )


# --------------------------------------------------------------------------
# Edit metadata / delete
# --------------------------------------------------------------------------

@app.get("/paper/{paper_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        return RedirectResponse(url="/")
    tag_string = ", ".join(tag.name for tag in paper.tags)
    return templates.TemplateResponse(
        request, "edit.html", {"paper": paper, "tag_string": tag_string}
    )


@app.post("/paper/{paper_id}/edit")
def edit_save(
    paper_id: int,
    title: str = Form(...),
    authors: str = Form(""),
    journal: str = Form(""),
    year: str = Form(""),
    doi: str = Form(""),
    status: str = Form("unread"),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    paper = db.get(Paper, paper_id)
    if paper is None:
        return RedirectResponse(url="/", status_code=303)

    paper.title = title.strip() or paper.title
    paper.authors = authors.strip() or None
    paper.journal = journal.strip() or None
    paper.year = int(year) if year.strip().isdigit() else None
    paper.doi = doi.strip() or None
    if status in ("unread", "reading", "read"):
        paper.status = status

    # Tags arrive as a comma-separated string; stored lowercase and
    # created on first use.
    paper.tags.clear()
    for name in {t.strip().lower() for t in tags.split(",") if t.strip()}:
        tag = db.query(Tag).filter(Tag.name == name).first()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
        paper.tags.append(tag)

    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/paper/{paper_id}/delete")
def delete_paper(paper_id: int, db: Session = Depends(get_db)):
    """Remove the paper row and its files on disk."""
    paper = db.get(Paper, paper_id)
    if paper is not None:
        for path in (paper.pdf_path, paper.html_path):
            if path:
                Path(path).unlink(missing_ok=True)
        shutil.rmtree(FIGURE_DIR / str(paper.id), ignore_errors=True)
        db.delete(paper)
        db.commit()
    return RedirectResponse(url="/", status_code=303)
