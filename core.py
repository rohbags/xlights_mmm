"""Read-only xLights sequence discovery and targeted MP3/M4A metadata updates.

No function in this module opens an XSQ for writing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
import os
import html
import xml.etree.ElementTree as ET

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, COMM
from mutagen.mp4 import MP4

FIELDS = ("song", "artist", "album", "comment")
MP4_TAGS = {"song": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "comment": "\xa9cmt"}
ID3_TAGS = {"song": "TIT2", "artist": "TPE1", "album": "TALB", "comment": "COMM"}
SUPPORTED_EXTENSIONS = {".mp3", ".m4a"}


@dataclass
class Record:
    sequence: str
    media: str = ""
    expected: dict[str, str] = field(default_factory=dict)
    actual: dict[str, str] = field(default_factory=dict)
    status: str = "Not checked"
    detail: str = ""
    selected: bool = True
    readable: bool = False

    def __post_init__(self):
        for key in FIELDS:
            self.expected.setdefault(key, "")
            self.actual.setdefault(key, "")


def normalize_xsq_text(value: str) -> str:
    """Decode extra HTML/XML-entity escaping in xLights metadata text.

    ElementTree has already handled the XML escaping layer. Some XSQ values
    are double-escaped (e.g. &amp;amp; in the raw XML); this removes the
    remaining escaped layer(s) before comparison AND writing. Never apply
    this to mediaFile paths or other non-metadata elements.
    """
    for _ in range(5):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def read_xsq_header(path: Path) -> dict[str, str]:
    """Only open as rb. Stop XML parsing once </head> is reached."""
    with path.open("rb") as source:
        parser = ET.iterparse(source, events=("end",))
        for _, elem in parser:
            if elem.tag == "head":
                return {child.tag: (child.text or "") for child in elem}
            # The head is the first element of interest; avoid retaining
            # everything else when encountering malformed/unexpected files.
    raise ValueError("Missing <head> section")


def discover_sequences(directory: Path, recursive: bool) -> list[Path]:
    paths = directory.rglob("*") if recursive else directory.iterdir()
    return sorted((p for p in paths if p.is_file() and p.suffix.lower() == ".xsq"),
                  key=lambda p: str(p).casefold())


def read_tags(path: Path) -> dict[str, str]:
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            return {key: "" for key in FIELDS}
        result = {}
        for key in ("song", "artist", "album"):
            frame = tags.get(ID3_TAGS[key])
            result[key] = str(frame.text[0]) if frame is not None and frame.text else ""
        comments = tags.getall("COMM")
        # A nonempty alternate COMM must not be hidden behind a blank
        # preferred COMM. Otherwise clear-blank verification can show Match
        # even though other programs still display the nonempty comment.
        nonempty = [c for c in comments if c.text and any(str(t) for t in c.text)]
        candidates = nonempty or comments
        preferred = next((c for c in candidates if c.desc == "" and c.lang == "eng"), None)
        if preferred is None:
            preferred = next((c for c in candidates if c.desc == ""), None)
        if preferred is None and candidates:
            preferred = candidates[0]
        result["comment"] = str(preferred.text[0]) if preferred is not None and preferred.text else ""
        return result
    if ext == ".m4a":
        audio = MP4(str(path))
        tags = audio.tags or {}
        result = {}
        for key, atom in MP4_TAGS.items():
            values = tags.get(atom, [])
            result[key] = str(values[0]) if values else ""
        return result
    raise ValueError(f"Unsupported audio extension: {ext}")


def desired_fields(expected: dict[str, str], clear_blanks: bool) -> dict[str, str]:
    return {key: expected.get(key, "") for key in FIELDS
            if clear_blanks or expected.get(key, "") != ""}


def differences(expected: dict[str, str], actual: dict[str, str], clear_blanks: bool) -> dict[str, tuple[str, str]]:
    return {key: (wanted, actual.get(key, ""))
            for key, wanted in desired_fields(expected, clear_blanks).items()
            if wanted != actual.get(key, "")}


def status_from_values(record: Record, clear_blanks: bool) -> tuple[str, str]:
    if record.status in {"Missing media", "Invalid media path", "Unsupported media", "XSQ error", "Media error", "Conflict"}:
        return record.status, record.detail
    diff = differences(record.expected, record.actual, clear_blanks)
    if not diff:
        return "Match", "All applicable metadata matches. Blank XSQ fields are ignored." if not clear_blanks else "All metadata matches."
    return "Different", "; ".join(f"{key.title()}: XSQ={want!r}, audio={got!r}" for key, (want, got) in diff.items())


def scan_sequence(path: Path, clear_blanks: bool) -> Record | None:
    try:
        head = read_xsq_header(path)
    except Exception as exc:
        return Record(sequence=str(path), status="XSQ error", detail=f"Cannot read XSQ header: {exc}")
    if head.get("sequenceType", "").strip().casefold() != "media":
        return None
    record = Record(sequence=str(path), media=head.get("mediaFile", "").strip(),
                    expected={key: normalize_xsq_text(head.get(key, "")) for key in FIELDS})
    if not record.media or not Path(record.media).is_absolute():
        record.status = "Invalid media path"
        record.detail = "<mediaFile> must contain a nonempty absolute path."
        return record
    media_path = Path(record.media)
    if media_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        record.status = "Unsupported media"
        record.detail = "Only .mp3 and .m4a are supported."
        return record
    if not media_path.is_file():
        record.status = "Missing media"
        record.detail = f"File not found: {record.media}"
        return record
    try:
        record.actual = read_tags(media_path)
        record.readable = True
        record.status, record.detail = status_from_values(record, clear_blanks)
    except Exception as exc:
        record.status = "Media error"
        record.detail = f"Cannot read audio tags: {exc}"
    return record


def prepare_jobs(records: list[Record], indices: list[int], clear_blanks: bool):
    """Group selected rows by media file, blocking inconsistent duplicate edits."""
    jobs: list[tuple[str, dict[str, str], list[int]]] = []
    failures: dict[int, tuple[str, str]] = {}
    groups: dict[str, list[int]] = {}
    for index in indices:
        record = records[index]
        if record.status in {"XSQ error", "Missing media", "Invalid media path", "Unsupported media"}:
            failures[index] = (record.status, record.detail)
        else:
            groups.setdefault(os.path.normcase(os.path.realpath(record.media)), []).append(index)
    for canonical, members in groups.items():
        merged: dict[str, str] = {}
        conflicting_fields: set[str] = set()
        for index in members:
            for field_name, value in desired_fields(records[index].expected, clear_blanks).items():
                if field_name in merged and merged[field_name] != value:
                    conflicting_fields.add(field_name)
                else:
                    merged[field_name] = value
        if conflicting_fields:
            description = "Conflicting XSQ values for the same audio file: " + ", ".join(sorted(conflicting_fields))
            for index in members:
                failures[index] = ("Conflict", description)
        else:
            jobs.append((records[members[0]].media, merged, members))
    return jobs, failures


def write_tags(path: Path, changes: dict[str, str]) -> None:
    """Update ONLY the four supported fields, preserving all unrelated tags."""
    if not changes:
        return
    if path.suffix.lower() == ".mp3":
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        frames = {"song": TIT2, "artist": TPE1, "album": TALB}
        for name, value in changes.items():
            frame_id = ID3_TAGS[name]
            if name == "comment":
                # Explicitly changing/clearing the Comment field must update
                # ALL ID3 comment frames. Some existing MP3s contain only
                # nonstandard/described COMM frames; leaving them behind
                # makes read-back return the old comment after a clear.
                # This never executes for blank XSQ comments unless the
                # user explicitly enables the clear-blank-fields option.
                tags.delall("COMM")
                if value:
                    tags.add(COMM(encoding=1, lang="eng", desc="", text=[value]))
            else:
                tags.delall(frame_id)
                if value:
                    tags.add(frames[name](encoding=1, text=[value]))
        tags.save(str(path), v2_version=3)
    elif path.suffix.lower() == ".m4a":
        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        for name, value in changes.items():
            atom = MP4_TAGS[name]
            if value:
                audio.tags[atom] = [value]
            else:
                audio.tags.pop(atom, None)
        audio.save()
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")


def apply_one(path: Path, wanted: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Read, conditionally write, then independently reread and verify."""
    before = read_tags(path)
    changes = {key: value for key, value in wanted.items() if before.get(key, "") != value}
    # For an explicitly requested blank comment, remove ALL COMM frames,
    # including nonstandard/empty ones. An empty preferred frame alone does
    # not mean the file is free of comment frames.
    if wanted.get("comment", None) == "" and path.suffix.lower() == ".mp3":
        try:
            if ID3(str(path)).getall("COMM"):
                changes["comment"] = ""
        except ID3NoHeaderError:
            pass
    if changes:
        write_tags(path, changes)
    after = read_tags(path)
    remaining = {key: (value, after.get(key, "")) for key, value in wanted.items()
                 if after.get(key, "") != value}
    if remaining:
        raise ValueError("Read-back verification failed: " + str(remaining))
    return ("Written & verified" if changes else "Match"), after
