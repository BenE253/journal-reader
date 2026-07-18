# PaperShelf

A personal, single-user web app for reading medical journal papers on your
phone. Upload a PDF, and PaperShelf converts it with
[Marker](https://github.com/VikParuchuri/marker) into clean, single-column,
phone-readable HTML — with a searchable library, reading progress, figure
lightbox, and collapsible references.

**Phase 1** (this version): upload → convert → read pipeline, library with
search/filters, mobile reader with progress tracking and dark mode.
Coming next: highlights (Phase 2), Zotero sync (Phase 3), PWA/offline (Phase 4).

## Setup

Requires **Python 3.11+**. From the project folder:

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
#    Note: marker-pdf pulls in PyTorch, so this download is large (several GB)
#    and takes a while. Get coffee.
pip install -r requirements.txt

# 3. Run the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open <http://localhost:8000>.

The SQLite database and all files live under `storage/` (created
automatically on first run — nothing to configure).

### Marker models

Marker downloads its ML models (~3 GB) automatically the **first time a
conversion runs** — so your first upload will sit in "converting…" noticeably
longer than usual. Subsequent conversions typically take 1–5 minutes per
paper depending on your hardware (much faster with a GPU).

### Seeding a starting library

Drop PDFs into a `seed/` folder next to `main.py`, then:

```bash
python seed.py
```

Each PDF is imported and converted one at a time, with progress printed to
the terminal. Re-running the script skips papers already imported (matched
by DOI).

## Reading from your iPhone (Tailscale)

1. Install [Tailscale](https://tailscale.com) on the machine running
   PaperShelf and on your iPhone, signed into the same tailnet.
2. Start the server with `--host 0.0.0.0` (as above) so it accepts
   connections from other devices.
3. Find the machine's Tailscale name or IP (`tailscale status` or the
   admin console — something like `100.x.y.z` or `mymachine.tailnet-name.ts.net`).
4. On your phone, open `http://<tailscale-ip>:8000` in Safari.
5. Share button → **Add to Home Screen** for an app-like icon. (Full PWA
   install with offline reading arrives in Phase 4.)

Tailscale traffic is end-to-end encrypted and the app is never exposed to
the public internet, which is why the app itself has no login screen.

## How it works

| File | Role |
| --- | --- |
| `main.py` | FastAPI app and all routes |
| `models.py` | SQLAlchemy models (papers, figures, highlights, tags, settings) |
| `database.py` | SQLite engine/session setup, table creation |
| `convert.py` | Marker pipeline: PDF → Markdown → HTML, figure + caption extraction |
| `metadata.py` | DOI regex extraction + Crossref lookup |
| `seed.py` | Bulk-import PDFs from `seed/` |
| `templates/`, `static/` | Jinja2 templates, CSS, vanilla JS |

Conversion runs as a FastAPI background task: the paper shows a spinner in
the library, and the page refreshes itself when conversion finishes. If
Marker fails on a PDF (it happens — scanned files, odd layouts), the paper
is marked **failed** with the error message and a retry button.

## Troubleshooting

- **Conversion fails immediately with an ImportError** — `marker-pdf`
  isn't installed in the active environment. Re-run
  `pip install -r requirements.txt` inside the venv.
- **Conversion is killed / the server dies mid-conversion** — Marker ran
  out of memory. Close other apps or add swap; 8 GB+ RAM recommended.
  Papers stuck "processing" after a crash are marked failed on restart so
  you can retry them.
- **No metadata found** — the PDF had no extractable DOI. Open the paper's
  **Edit** page and fill the fields in by hand.
