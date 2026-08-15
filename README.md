# WQPDFExtractor

A browser-based tool that extracts content from online book readers and compiles them into local PDF files. It uses Playwright to automate a real Chromium browser, supporting both PDF image readers and EPUB HTML readers.

## Features

- **Dual reader support** — handles both PDF image readers and EPUB HTML readers automatically
- **Automatic detection** — monitors browser tabs and starts extraction when a reader page is opened
- **Parallel extraction** — supports multiple tabs extracting different books simultaneously
- **PDF reader**:
  - Network interception to capture image slices directly
  - Slice reassembly with CSS layout positioning
  - Rotation correction via CSS transform matrix detection (0/90/180/270)
  - Auto-retry on 60s stall with page reload
  - Bookmarks extracted from table of contents (nested chapters supported)
- **EPUB reader** (experimental):
  - Extracts decrypted HTML content from the rendered iframe
  - Downloads all external resources (CSS, images, fonts, etc.) into per-chapter folders
  - Rewrites CSS `url()` references to local paths
  - Converts chapters to A4-sized PDF pages
  - Automatic single-column layout switching
  - Bookmarks not yet supported
- **Crash recovery** — resumes from previously downloaded content on restart
- **Trial detection** — stops and notifies when trial reading limit is reached
- **Persistent session** — browser data saved locally, no repeated logins

## Requirements

- Python 3.11+
- Chromium (installed via Playwright)

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python main.py            # resume mode (skips already downloaded content)
python main.py --force    # re-download everything from scratch
```

1. A Chromium window opens and navigates to the bookshelf page.
2. Log in manually if needed.
3. Open any book's reader page — extraction starts automatically.
4. Open multiple books in separate tabs for parallel extraction.
5. Close the browser window to exit.

## Output Structure

```
output/
└── {book_id}/
    ├── images/              # PDF reader: reassembled page images
    │   ├── 0001.png
    │   └── ...
    ├── html/                # EPUB reader: per-chapter HTML + resources
    │   ├── 0001/
    │   │   ├── index.html
    │   │   └── assets/
    │   │       ├── stylesheet.css
    │   │       ├── image1.png
    │   │       └── ...
    │   ├── 0002/
    │   │   └── ...
    │   └── ...
    └── {book_title}.pdf
```

Intermediate downloads are stored in a temporary directory (`%TEMP%/WQPDFExtractor/`) and copied to `output/` only after each page/chapter is fully processed. This ensures incomplete data from a crash is not mistaken for finished content on the next run.
