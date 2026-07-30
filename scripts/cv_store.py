#!/usr/bin/env python
"""CV versions under config/profile. Staged, confirmed, never overwritten.

The scorer reads whichever CV the active marker points at. Uploads arrive
through the served dashboard, get extracted to markdown, sit as a pending
file until a human confirms the preview, and only then become the active
version. Every accepted version stays on disk with a dated name, history
is append-only, and the whole directory is gitignored like its neighbours.

Layout inside config/profile/:
  cv-20260729-213045.md   accepted versions, dated, never overwritten
  cv.active               one line, the filename of the version in use
  pending-cv-<token>.md   staged uploads awaiting confirm, pruned after a day

Standard library throughout. docx is a zip of XML and reads fine without
python-docx. pdf goes through pypdf when it is installed, the same optional
dependency load_cv_text already uses.
"""

from __future__ import annotations

import re
import secrets
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common

PROFILE_DIR = radar_common.REPO_ROOT / "config" / "profile"
MARKER_NAME = "cv.active"
MAX_UPLOAD_BYTES = 5_000_000
PENDING_MAX_AGE_SECONDS = 24 * 3600

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,120}$")

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class UploadError(RuntimeError):
    """A problem worth showing the human verbatim."""


# -------------------------------------------------------------- extraction

MAX_DOCX_XML_BYTES = 20_000_000  # decompressed cap, a zip bomb stops here


def _docx_to_markdown(data: bytes) -> str:
    """word/document.xml paragraphs to markdown, headings and bullets kept."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            # The upload cap bounds the compressed size only. A crafted
            # zip can declare gigabytes behind a 5 MB face, so the
            # decompressed size is checked before a byte is inflated.
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise UploadError(
                    "The docx declares an implausibly large document "
                    "inside, refusing to inflate it.")
            xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as err:
        raise UploadError(f"Not a readable docx file. {err}") from err
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as err:
        raise UploadError(f"The docx XML would not parse. {err}") from err

    lines: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        text = "".join(t.text or "" for t in para.iter(f"{_W_NS}t")).strip()
        if not text:
            continue
        style = para.find(f"{_W_NS}pPr/{_W_NS}pStyle")
        style_val = (style.get(f"{_W_NS}val") or "") if style is not None else ""
        heading = re.match(r"[Hh]eading(\d)", style_val)
        numbered = para.find(f"{_W_NS}pPr/{_W_NS}numPr") is not None
        if heading:
            level = min(int(heading.group(1)), 6)
            lines.append("#" * level + " " + text)
        elif numbered:
            lines.append("- " + text)
        else:
            lines.append(text)
    if not lines:
        raise UploadError("The docx contained no readable text.")
    return "\n\n".join(lines) + "\n"


def _pdf_to_markdown(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as err:
        raise UploadError(
            "PDF uploads need pypdf. pip install pypdf, or upload the CV "
            "as docx, md or txt instead.") from err
    try:
        reader = PdfReader(BytesIO(data))
        text = "\n\n".join((page.extract_text() or "").strip()
                           for page in reader.pages)
    except Exception as err:  # noqa: BLE001  pypdf raises a small zoo
        raise UploadError(f"The PDF would not read. {type(err).__name__}: {err}") from err
    if not text.strip():
        raise UploadError(
            "The PDF yielded no text, it is probably a scan. Upload a "
            "text-based export instead.")
    return text.strip() + "\n"


def extract_markdown(filename: str, data: bytes) -> str:
    """Uploaded bytes to markdown text, by extension. Loud on anything odd."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"The file is over {MAX_UPLOAD_BYTES // 1_000_000} MB, "
                          "which no CV needs to be.")
    if not data:
        raise UploadError("The upload arrived empty.")
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        return data.decode("utf-8-sig", errors="replace").strip() + "\n"
    if suffix == ".docx":
        return _docx_to_markdown(data)
    if suffix == ".pdf":
        return _pdf_to_markdown(data)
    raise UploadError(f"Unsupported file type {suffix or '(none)'}. "
                      "md, txt, docx and pdf are accepted.")


# ------------------------------------------------------------- the store

def _prune_pending(base: Path) -> None:
    cutoff = time.time() - PENDING_MAX_AGE_SECONDS
    for stale in base.glob("pending-cv-*.md"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass


def stage(text: str, base: Path | None = None) -> str:
    """Park extracted markdown as a pending file. Returns its token."""
    base = base or PROFILE_DIR
    base.mkdir(parents=True, exist_ok=True)
    _prune_pending(base)
    token = secrets.token_hex(8)
    (base / f"pending-cv-{token}.md").write_text(text, encoding="utf-8")
    return token


def _pending_path(token: str, base: Path) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        raise UploadError("That upload token is not one this server issued.")
    path = base / f"pending-cv-{token}.md"
    if not path.exists():
        raise UploadError("Nothing pending under that token. It may have "
                          "been confirmed, discarded or pruned already.")
    return path


def read_pending(token: str, base: Path | None = None) -> str:
    return _pending_path(token, base or PROFILE_DIR).read_text(encoding="utf-8")


def discard(token: str, base: Path | None = None) -> None:
    _pending_path(token, base or PROFILE_DIR).unlink()


# One confirm at a time. The dated filename plus exclusive create below
# already refuse to reuse a name, the lock keeps two same-second confirms
# from racing the exists-check at all under the threading server.
_CONFIRM_LOCK = threading.Lock()


def confirm(token: str, base: Path | None = None) -> dict:
    """Promote a pending upload to the active CV. History stays whole.

    The dated filename is unique by construction and never reused. The
    file is created with exclusive open, so even a race that slipped the
    lock would raise rather than overwrite, history is append-only at the
    filesystem's own insistence. The previous version file is not
    touched, only the marker moves.
    """
    base = base or PROFILE_DIR
    with _CONFIRM_LOCK:
        pending = _pending_path(token, base)
        text = pending.read_text(encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        counter = 1
        while True:
            name = (f"cv-{stamp}.md" if counter == 1
                    else f"cv-{stamp}-{counter}.md")
            dest = base / name
            try:
                with open(dest, "x", encoding="utf-8") as fh:
                    fh.write(text)
                break
            except FileExistsError:
                counter += 1
        (base / MARKER_NAME).write_text(dest.name + "\n", encoding="utf-8")
        pending.unlink()
        return {"file": dest.name, "version": dest.name}


def active_cv_name(base: Path | None = None) -> str | None:
    """The filename the marker points at, when it points at a real file."""
    base = base or PROFILE_DIR
    marker = base / MARKER_NAME
    if not marker.exists():
        return None
    name = marker.read_text(encoding="utf-8").strip()
    if not name or not _SAFE_NAME.match(name) or not (base / name).exists():
        print(f"WARNING. {MARKER_NAME} points at {name!r} which is missing "
              "or unsafe. Falling back to cv.txt or cv.pdf.", file=sys.stderr)
        return None
    return name


def history(base: Path | None = None) -> list[str]:
    base = base or PROFILE_DIR
    return sorted((p.name for p in base.glob("cv-*.md")), reverse=True)
