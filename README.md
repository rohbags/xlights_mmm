# xLights Media Metadata Manager

Bugfix release 1.3: normalizes double-escaped XML entities in XSQ metadata (such as `&amp;amp;` and `&amp;apos;`) before comparison/writing, and reliably removes all ID3 comment frames when clearing blank comments is enabled. Retains v1.2 Qt thread-lifetime fixes. Other unrelated ID3 frames and M4A atoms remain preserved.

A desktop GUI to copy **Song (Title), Artist, Album and Comment** metadata from xLights `.xsq` sequences into their referenced `.mp3` or `.m4a` files, then independently verify the tags.

## XSQ safety (critical)

The application **never modifies XSQ sequence files**. The only XSQ access is `Path.open('rb')` for XML parsing; there is no XSQ writing, deletion, renaming or saving functionality. Audio files are modified only when you confirm **Apply Metadata**. Still maintain normal backups of valuable audio and show data.

## Installation

Requires Python 3.10+ and a GUI desktop (Linux or Windows).

```bash
python -m pip install -r requirements.txt
python app.py
```

On Fedora, you may prefer a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Usage

1. Click **Browse** and choose the directory containing `.xsq` files. Enable recursive scanning if needed.
2. Click **Scan**. Only files whose header contains `<sequenceType>Media</sequenceType>` are included; unreadable XSQ files are shown as errors. Each row shows the **actual XSQ filename**, media filename, title, artist, album, comment and status.
3. All rows are selected initially; uncheck unwanted rows, use **Select none**, or **Select differences**.
4. Click **Confirm Metadata (read only)** to reread the audio tags from disk and compare them against the XSQ fields. Double-click a row to inspect its full paths and tag values.
5. Click **Apply Metadata**, review the confirmation dialog, and approve. The app writes only mismatching fields to each unique referenced audio file, then rereads it to verify the result. There is a progress bar and per-file failure reporting.

### Blank fields

By default a blank field in the XSQ **does not overwrite** or count against the corresponding audio tag. Checking **Write blank XSQ fields** instead clears the respective audio tags, and compares blank fields as empty during verification.

### Media paths

The `<mediaFile>` value must contain an **absolute path**, and is used as-is. There is no media search or path rewriting. Supported extensions: `.mp3` and `.m4a`.

### Conflicting sequences

If two selected sequences point at the same audio file but request different values for the same tag, their write is blocked and reported as a conflict. Compatible references to the same audio file are written once. Files already matching are not rewritten.

### MP3 comments

ID3 `COMM` comments are read, including nonstandard/described comments. Existing comments are preserved when the XSQ comment is blank and **Write blank XSQ fields** is unchecked. When writing a new comment or explicitly clearing it, existing `COMM` frames (including described comments) are replaced or removed so read-back verification is reliable. MP3 tags are written in ID3v2.3 format; other unrelated frames and embedded artwork are retained. M4A metadata uses standard MP4 atoms (`©nam`, `©ART`, `©alb`, `©cmt`).

## Notes

- Audio writes are in-place metadata updates by Mutagen; they do **not** re-encode audio, but any file update can fail due to storage, permissions, or corruption. Back up important audio files before bulk operations.
- A missing or unsupported audio file is never written.
- Scanning intentionally parses only the XML `<head>` section of a sequence to minimize memory use with large shows.
- Confirmation/verification never writes to either XSQ or audio files.
- When writing, the program counts **distinct audio files**, avoiding duplicate writes where multiple XSQs reference the same track.

## v1.2: QThread shutdown fix

Fixed a crash (`QThread: Destroyed while thread '' is still running`) that could
occur when Apply Metadata processed a batch where **every audio file was already
matched**. The GUI now retains ownership of the worker until Qt emits its
`finished` signal, waits for thread shutdown, and only then shows the completion
dialog. This change does not alter media tagging or XSQ parsing.
