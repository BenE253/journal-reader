"""DOI extraction and Crossref metadata lookup.

Flow (see main.py's upload handler):
  1. Pull text from the first few PDF pages with pypdf (fast, no ML).
  2. Regex out a DOI if one is present.
  3. Ask Crossref for title/authors/journal/year.
  4. If any step fails, the caller falls back to Marker's title detection
     (after conversion) and finally the filename. The user can always edit
     metadata by hand.
"""

import re
from dataclasses import dataclass

import httpx
from pypdf import PdfReader

# DOIs start "10.<registrant>/<suffix>". The suffix can contain almost
# anything, so we grab non-whitespace and trim trailing punctuation that is
# usually sentence punctuation rather than part of the DOI.
DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)

CROSSREF_URL = "https://api.crossref.org/works/{doi}"
# Crossref asks polite API users to identify themselves.
USER_AGENT = "PaperShelf/0.1 (personal paper reader; mailto:papershelf@example.com)"


@dataclass
class PaperMetadata:
    """Metadata fields we try to fill automatically."""

    title: str | None = None
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None


def extract_doi(pdf_path: str, max_pages: int = 3) -> str | None:
    """Return the first DOI found in the opening pages of the PDF, or None.

    The DOI almost always appears on page 1 (header/footer or citation
    line), so scanning a few pages keeps this fast even for long papers.
    """
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return None  # corrupt/encrypted PDF; not fatal, just no DOI

    for page in reader.pages[:max_pages]:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        match = DOI_PATTERN.search(text)
        if match:
            # Strip punctuation that commonly trails a DOI in running text.
            return match.group(1).rstrip(".,;)]}”’")
    return None


def fetch_crossref(doi: str) -> PaperMetadata | None:
    """Look up a DOI on Crossref. Returns None on any network/parse error."""
    try:
        response = httpx.get(
            CROSSREF_URL.format(doi=doi),
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        work = response.json()["message"]
    except Exception:
        return None

    return PaperMetadata(
        title=(work.get("title") or [None])[0],
        authors=_format_authors(work.get("author") or []),
        journal=(work.get("container-title") or [None])[0],
        year=_extract_year(work),
        doi=doi,
    )


def _format_authors(authors: list[dict]) -> str | None:
    """Build a compact display string like "Elsbernd B, et al."."""
    names = []
    for author in authors:
        family = author.get("family")
        given = author.get("given", "")
        if not family:
            continue
        # First initial only, matching common medical citation style.
        initial = given[0] if given else ""
        names.append(f"{family} {initial}".strip())
    if not names:
        return None
    if len(names) > 2:
        return f"{names[0]}, et al."
    return ", ".join(names)


def _extract_year(work: dict) -> int | None:
    """Crossref nests the year as issued.date-parts = [[year, month, day]]."""
    try:
        return int(work["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def title_from_filename(filename: str) -> str:
    """Last-resort title: the filename without extension, underscores as
    spaces. Better than an empty title; the user can edit it."""
    stem = filename.rsplit(".", 1)[0]
    return stem.replace("_", " ").replace("-", " ").strip() or "Untitled paper"
