"""PDF -> reader HTML conversion pipeline using Marker.

Runs as a FastAPI background task. The pipeline:

  1. Marker converts the PDF to Markdown and extracts figure images.
  2. Figures are saved to storage/figures/{paper_id}/ and captions are
     parsed from the Markdown lines surrounding each image reference.
  3. Markdown is rendered to HTML with the `markdown` package.
  4. A BeautifulSoup post-processing pass rewrites image srcs, wraps
     tables in horizontally-scrollable divs, and collapses the References
     section into a <details> element.
  5. The finished HTML fragment is written to storage/html/{paper_id}.html.

Marker is imported lazily inside the task so the app still starts (and
uploads still work) on a machine where Marker isn't installed yet — the
paper just ends up in the "failed" state with a clear error message and a
retry button.
"""

import re
import traceback
from datetime import datetime
from pathlib import Path

import ftfy
import markdown as md
from bs4 import BeautifulSoup

from database import FIGURE_DIR, HTML_DIR, SessionLocal
from models import Figure, Paper

# Marker's model load takes tens of seconds and several GB of RAM, so we
# build the converter once and reuse it for every paper in this process.
_converter = None


def _get_converter():
    """Lazily create and cache the Marker PdfConverter."""
    global _converter
    if _converter is None:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        _converter = PdfConverter(artifact_dict=create_model_dict())
    return _converter


def convert_paper(paper_id: int) -> None:
    """Background task entry point: convert one paper end to end.

    Opens its own DB session (background tasks run outside the request's
    session) and records success/failure on the paper row.
    """
    db = SessionLocal()
    try:
        paper = db.get(Paper, paper_id)
        if paper is None:
            return

        paper.conversion_status = "processing"
        paper.conversion_error = None
        db.commit()

        try:
            _run_pipeline(db, paper)
            paper.conversion_status = "done"
            db.commit()
        except Exception as exc:  # noqa: BLE001 - any failure marks the paper failed
            db.rollback()
            paper = db.get(Paper, paper_id)
            if paper is not None:
                paper.conversion_status = "failed"
                # Keep a short, human-readable reason for the library UI.
                paper.conversion_error = f"{type(exc).__name__}: {exc}"[:1000]
                db.commit()
            traceback.print_exc()
    finally:
        db.close()


def _run_pipeline(db, paper: Paper) -> None:
    """The actual conversion steps. Raises on failure."""
    from marker.output import text_from_rendered

    converter = _get_converter()
    rendered = converter(paper.pdf_path)
    # text: the Markdown string; images: {filename: PIL.Image}
    text, _format, images = text_from_rendered(rendered)
    # Repair symbol-font damage (garbled µg, α, β, ± …) before anything
    # downstream — captions and HTML both come from this string.
    text = _clean_text(text)

    figure_dir = FIGURE_DIR / str(paper.id)
    figure_dir.mkdir(parents=True, exist_ok=True)

    # Save extracted images to disk and record them with parsed captions.
    captions = _parse_captions(text)
    # Replace any figures from a previous (failed/retried) conversion.
    for old in list(paper.figures):
        db.delete(old)
    for order, (name, image) in enumerate(images.items()):
        image_path = figure_dir / name
        image.save(image_path)
        db.add(
            Figure(
                paper_id=paper.id,
                image_path=str(image_path),
                caption=captions.get(name),
                display_order=order,
            )
        )

    html = _markdown_to_html(text, paper.id)
    html_path = HTML_DIR / f"{paper.id}.html"
    html_path.write_text(html, encoding="utf-8")
    paper.html_path = str(html_path)

    # If metadata fell back to the filename, try to do better with the
    # first heading Marker found in the document.
    marker_title = _first_heading(text)
    if marker_title and _looks_like_filename_title(paper):
        paper.title = marker_title

    db.commit()


# --------------------------------------------------------------------------
# Text cleanup (garbled Greek letters & symbols)
# --------------------------------------------------------------------------

# Journal PDFs frequently set Greek letters and math symbols in "symbol"
# fonts. When such a font lacks a proper Unicode mapping, extraction
# yields characters from the Private Use Area (U+F0xx), which browsers
# draw as empty boxes. The PUA code equals the character's slot in
# Adobe's classic Symbol font, so the intended character is recoverable:
SYMBOL_FONT_MAP = str.maketrans({
    # lowercase greek (Symbol-font slot = Latin letter position)
    "\uf061": "α",
    "\uf062": "β",
    "\uf063": "χ",
    "\uf064": "δ",
    "\uf065": "ε",
    "\uf066": "φ",
    "\uf067": "γ",
    "\uf068": "η",
    "\uf069": "ι",
    "\uf06b": "κ",
    "\uf06c": "λ",
    "\uf06d": "μ",
    "\uf06e": "ν",
    "\uf070": "π",
    "\uf071": "θ",
    "\uf072": "ρ",
    "\uf073": "σ",
    "\uf074": "τ",
    "\uf075": "υ",
    "\uf077": "ω",
    "\uf078": "ξ",
    "\uf079": "ψ",
    "\uf07a": "ζ",
    # uppercase greek
    "\uf044": "Δ",
    "\uf046": "Φ",
    "\uf047": "Γ",
    "\uf04c": "Λ",
    "\uf050": "Π",
    "\uf051": "Θ",
    "\uf053": "Σ",
    "\uf057": "Ω",
    "\uf058": "Ξ",
    "\uf059": "Ψ",
    # common math/units symbols
    "\uf0b0": "°",
    "\uf0b1": "±",
    "\uf0b3": "≥",
    "\uf0a3": "≤",
    "\uf0b9": "≠",
    "\uf0bb": "≈",
    "\uf0b4": "×",
    "\uf0b8": "÷",
    "\uf0ae": "→",
    "\uf0ac": "←",
})


def _clean_text(text: str) -> str:
    """Fix the two common ways Greek/symbols get garbled in extraction.

    1. Mojibake — UTF-8 read with the wrong encoding somewhere upstream,
       e.g. "Î¼g" instead of "µg". ftfy detects and reverses this.
    2. Symbol-font private-use codes (the empty-box characters) — mapped
       back to the intended Greek letter or symbol via the table above.

    (A third failure mode — the PDF genuinely containing a wrong letter,
    like "lg" for "µg" — is invisible to us and can't be fixed here.)
    """
    text = ftfy.fix_text(text)
    return text.translate(SYMBOL_FONT_MAP)


# --------------------------------------------------------------------------
# Caption parsing
# --------------------------------------------------------------------------

# Markdown image reference like ![](_page_2_Figure_3.jpeg)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# Lines that look like figure/table captions.
CAPTION_RE = re.compile(r"^(figure|fig\.?|table)\s*\d", re.IGNORECASE)


def _parse_captions(markdown_text: str) -> dict[str, str]:
    """Map image filename -> caption text.

    Marker emits each figure as an image line; the caption is usually the
    nearest non-empty line after it (sometimes before). We look ahead up
    to two non-empty lines, then behind one, accepting the first line that
    starts like "Figure 1" / "Table 2".
    """
    lines = markdown_text.splitlines()
    captions: dict[str, str] = {}

    for i, line in enumerate(lines):
        match = IMAGE_RE.search(line)
        if not match:
            continue
        image_name = match.group(1)

        candidates: list[str] = []
        # Up to two non-empty lines after the image...
        for follow in lines[i + 1 :]:
            stripped = follow.strip()
            if stripped:
                candidates.append(stripped)
            if len(candidates) == 2:
                break
        # ...and one non-empty line before it.
        for prev in reversed(lines[:i]):
            stripped = prev.strip()
            if stripped:
                candidates.append(stripped)
                break

        for candidate in candidates:
            if CAPTION_RE.match(candidate):
                captions[image_name] = candidate
                break

    return captions


# --------------------------------------------------------------------------
# Markdown -> HTML and post-processing
# --------------------------------------------------------------------------

REFERENCE_HEADINGS = {"references", "bibliography", "literature cited", "works cited"}


def _markdown_to_html(markdown_text: str, paper_id: int) -> str:
    """Render Marker's Markdown to the HTML fragment the reader serves."""
    html = md.markdown(
        markdown_text,
        # tables: GitHub-style tables; sane_lists: predictable list parsing.
        extensions=["tables", "sane_lists"],
    )
    soup = BeautifulSoup(html, "html.parser")

    # Point image srcs at the /figures static mount.
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith(("http://", "https://", "/")):
            img["src"] = f"/figures/{paper_id}/{src}"
        img["loading"] = "lazy"

    # Tables get a scrollable wrapper so wide tables never force the whole
    # page to scroll horizontally on the phone.
    for table in soup.find_all("table"):
        wrapper = soup.new_tag("div", attrs={"class": "table-scroll"})
        table.wrap(wrapper)

    _collapse_references(soup)
    return str(soup)


def _collapse_references(soup: BeautifulSoup) -> None:
    """Fold everything from the References heading onward into <details>.

    Reference lists are long and rarely read linearly; collapsing them
    makes the end of the paper reachable in one flick.
    """
    ref_heading = None
    for heading in soup.find_all(["h1", "h2", "h3"]):
        if heading.get_text(strip=True).lower().rstrip(":") in REFERENCE_HEADINGS:
            ref_heading = heading  # keep the LAST match (ToC lines may match earlier)

    if ref_heading is None:
        return

    details = soup.new_tag("details", attrs={"class": "references"})
    summary = soup.new_tag("summary")
    summary.string = ref_heading.get_text(strip=True)
    details.append(summary)

    # Move the heading's following siblings (the reference list itself,
    # possibly several elements) inside the <details>.
    sibling = ref_heading.next_sibling
    while sibling is not None:
        next_sibling = sibling.next_sibling
        details.append(sibling.extract())
        sibling = next_sibling

    ref_heading.replace_with(details)


# --------------------------------------------------------------------------
# Title fallback helpers
# --------------------------------------------------------------------------

def _first_heading(markdown_text: str) -> str | None:
    """First markdown heading — Marker's best guess at the paper title."""
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if len(title) > 5:  # skip junk like "# 1"
                return title
    return None


def _looks_like_filename_title(paper: Paper) -> bool:
    """True if the title is just the (cleaned) filename, i.e. no DOI/Crossref
    metadata was found at upload time."""
    filename_stem = Path(paper.pdf_path).stem.replace("_", " ").replace("-", " ").strip()
    return paper.title.strip().lower() == filename_stem.lower()
