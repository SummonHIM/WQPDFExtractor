# WQPDFExtractor

A browser-based tool that extracts page images from online PDF readers and compiles them into local PDF files. It uses Playwright to automate a real Chromium browser, intercepts image slice network requests, reassembles them with correct layout and rotation, and outputs a complete PDF.

## Features

- **Automatic detection** — monitors browser tabs and starts extraction when a PDF reader page is opened
- **Parallel extraction** — supports multiple tabs extracting different books simultaneously
- **Network interception** — captures image data directly from network responses, no re-downloading needed
- **Slice reassembly** — handles pages split into multiple image fragments, positioned via CSS layout
- **Rotation correction** — detects CSS transform matrices and corrects page orientation (0°/90°/180°/270°)
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
python main.py
```

1. A Chromium window opens and navigates to the bookshelf page.
2. Log in manually if needed.
3. Open any book's PDF reader page — extraction starts automatically.
4. Open multiple books in separate tabs for parallel extraction.
5. Close the browser window to exit.

## Output Structure

```
output/
└── {book_title}/
    ├── images/
    │   ├── 0001.png
    │   ├── 0002.png
    │   └── ...
    └── {book_title}.pdf
```
