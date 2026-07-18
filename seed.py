"""Seed the library from PDFs dropped in the seed/ folder.

Usage:
    python seed.py

Each PDF in seed/ is copied into storage/pdfs/, given a paper record with
DOI/Crossref metadata, and converted with Marker synchronously (one at a
time — Marker is memory-hungry). Already-imported DOIs are skipped, so the
script is safe to re-run.
"""

import shutil
import sys
from pathlib import Path

from convert import convert_paper
from database import PDF_DIR, SessionLocal, init_db
from main import create_paper_from_pdf

SEED_DIR = Path(__file__).resolve().parent / "seed"


def main() -> None:
    init_db()
    SEED_DIR.mkdir(exist_ok=True)

    pdfs = sorted(SEED_DIR.glob("*.pdf")) + sorted(SEED_DIR.glob("*.PDF"))
    if not pdfs:
        print(f"No PDFs found in {SEED_DIR}. Drop some in and re-run.")
        sys.exit(0)

    print(f"Found {len(pdfs)} PDF(s) to import.\n")

    for pdf in pdfs:
        db = SessionLocal()
        try:
            # Copy (not move) so the seed folder keeps your originals.
            dest = PDF_DIR / pdf.name
            counter = 1
            while dest.exists():
                dest = PDF_DIR / f"{pdf.stem}_{counter}.pdf"
                counter += 1
            shutil.copy2(pdf, dest)

            paper = create_paper_from_pdf(db, dest)
            if paper.conversion_status == "done":
                print(f"~ Skipping (already imported): {paper.title}")
                continue

            print(f"> Converting: {paper.title}")
            print("  (first run downloads Marker's models — be patient)")
        finally:
            db.close()

        # convert_paper manages its own session and won't raise; failures
        # are recorded on the paper row.
        convert_paper(paper.id)

        db = SessionLocal()
        try:
            from models import Paper

            refreshed = db.get(Paper, paper.id)
            if refreshed.conversion_status == "done":
                print(f"  ✓ Done: {refreshed.title}\n")
            else:
                print(f"  ✗ Failed: {refreshed.conversion_error}\n")
        finally:
            db.close()

    print("Seeding finished. Start the app with: uvicorn main:app --host 0.0.0.0")


if __name__ == "__main__":
    main()
