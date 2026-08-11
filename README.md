# WQPDFExtractor

A browser-based tool that extracts page images from online PDF readers and compiles them into local PDF files. It uses Playwright to automate a real Chromium browser, intercepts image slice network requests, reassembles them with correct layout and rotation, and outputs a complete PDF with bookmarks.

## Features

- **Automatic detection** — monitors browser tabs and starts extraction when a PDF reader page is opened
- **Parallel extraction** — supports multiple tabs extracting different books simultaneously
- **Network interception** — captures image data directly from network responses, no re-downloading needed
- **Slice reassembly** — handles pages split into multiple image fragments, positioned via CSS layout
- **Rotation correction** — detects CSS transform matrices and corrects page orientation (0°/90°/180°/270°)
- **Bookmarks** — extracts the table of contents and embeds it as PDF bookmarks (including nested chapters)
- **Crash recovery** — resumes from previously downloaded pages on restart, only fetching missing ones
- **Auto-retry** — if a page stalls for over 60 seconds, automatically reloads and retries
- **Persistent session** — browser data is saved locally so you don't need to log in every time

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
python main.py            # resume mode (skips already downloaded pages)
python main.py --force    # re-download all pages from scratch
```

1. A Chromium window opens and navigates to the bookshelf page.
2. Log in manually if needed.
3. Open any book's PDF reader page — extraction starts automatically.
4. Open multiple books in separate tabs for parallel extraction.
5. Close the browser window to exit.

## Output Structure

```
output/
└── {book_id}/
    ├── images/
    │   ├── 0001.png
    │   ├── 0002.png
    │   └── ...
    └── {book_title}.pdf
```

Intermediate downloads are stored in a temporary directory (`%TEMP%/WQPDFExtractor/`) and copied to `output/` only after each page is fully processed. This ensures incomplete pages from a crash are not mistaken for finished ones on the next run.
