"""Magic-byte content sniffing, shaped like a Magika result."""

from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional

# How much of the stream to inspect. Container formats (zip-based OOXML) store
# their member names uncompressed in local file headers, so the marker we look
# for sits near the front of the file.
_HEAD_BYTES = 64 * 1024


@dataclass
class _Output:
    label: str = "unknown"
    mime_type: Optional[str] = None
    is_text: bool = False
    extensions: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class _Prediction:
    output: _Output = field(default_factory=_Output)
    score: float = 1.0
    dl: _Output = field(default_factory=_Output)


@dataclass
class MagikaResult:
    status: str = "error"
    prediction: _Prediction = field(default_factory=_Prediction)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def output(self) -> _Output:
        return self.prediction.output


def _hit(label, mime, extensions, is_text=False):
    return MagikaResult(
        status="ok",
        prediction=_Prediction(
            output=_Output(
                label=label,
                mime_type=mime,
                is_text=is_text,
                extensions=list(extensions),
                description=label,
            )
        ),
    )


_UNKNOWN = MagikaResult()

# (signature, offset, label, mime, extensions)
_SIGNATURES = [
    (b"%PDF-", 0, "pdf", "application/pdf", ["pdf"]),
    (b"\x89PNG\r\n\x1a\n", 0, "png", "image/png", ["png"]),
    (b"\xff\xd8\xff", 0, "jpeg", "image/jpeg", ["jpg", "jpeg"]),
    (b"GIF87a", 0, "gif", "image/gif", ["gif"]),
    (b"GIF89a", 0, "gif", "image/gif", ["gif"]),
    (b"BM", 0, "bmp", "image/bmp", ["bmp"]),
    (b"II*\x00", 0, "tiff", "image/tiff", ["tiff", "tif"]),
    (b"MM\x00*", 0, "tiff", "image/tiff", ["tiff", "tif"]),
    (b"fLaC", 0, "flac", "audio/flac", ["flac"]),
    (b"OggS", 0, "ogg", "audio/ogg", ["ogg"]),
    (b"ID3", 0, "mp3", "audio/mpeg", ["mp3"]),
    (b"{\\rtf", 0, "rtf", "text/rtf", ["rtf"], True),
    (b"\x1f\x8b", 0, "gzip", "application/gzip", ["gz"]),
    (b"7z\xbc\xaf\x27\x1c", 0, "sevenzip", "application/x-7z-compressed", ["7z"]),
]

# Markers found inside a zip container, in priority order.
_ZIP_MARKERS = [
    (
        b"mimetypeapplication/epub+zip",
        "epub",
        "application/epub+zip",
        ["epub"],
    ),
    (
        b"word/",
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ["docx"],
    ),
    (
        b"ppt/",
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ["pptx"],
    ),
    (
        b"xl/",
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ["xlsx"],
    ),
]


def _sniff_riff(head: bytes) -> MagikaResult:
    """RIFF containers: distinguish WAV from WebP."""
    fourcc = head[8:12]
    if fourcc == b"WAVE":
        return _hit("wav", "audio/x-wav", ["wav"])
    if fourcc == b"WEBP":
        return _hit("webp", "image/webp", ["webp"])
    return _UNKNOWN


def _sniff_ftyp(head: bytes) -> MagikaResult:
    """ISO base-media containers (MP4 family)."""
    brand = head[8:12]
    if brand in (b"M4A ", b"M4B "):
        return _hit("m4a", "audio/mp4", ["m4a"])
    if brand in (b"isom", b"iso2", b"mp42", b"avc1", b"M4V "):
        return _hit("mp4", "video/mp4", ["mp4"])
    return _UNKNOWN


def _sniff_zip(head: bytes) -> MagikaResult:
    for marker, label, mime, extensions in _ZIP_MARKERS:
        if marker in head:
            return _hit(label, mime, extensions)
    return _hit("zip", "application/zip", ["zip"])


def _sniff_text(head: bytes) -> MagikaResult:
    """Classify text-ish payloads that have no binary signature."""
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("utf-8", errors="ignore")
        except Exception:
            return _UNKNOWN

    # Anything with NUL bytes is not text.
    if "\x00" in text:
        return _UNKNOWN

    stripped = text.lstrip("﻿ \t\r\n")
    lowered = stripped[:1024].lower()

    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return _hit("html", "text/html", ["html", "htm"], is_text=True)
    if lowered.startswith("<?xml"):
        # RSS/Atom feeds are XML as far as content sniffing goes.
        return _hit("xml", "text/xml", ["xml"], is_text=True)
    if stripped[:1] in ("{", "["):
        return _hit("json", "application/json", ["json"], is_text=True)

    return _hit("txt", "text/plain", ["txt"], is_text=True)


def _identify(head: bytes) -> MagikaResult:
    if not head:
        return _UNKNOWN

    if head.startswith(b"RIFF"):
        result = _sniff_riff(head)
        if result.ok:
            return result

    if head[4:8] == b"ftyp":
        result = _sniff_ftyp(head)
        if result.ok:
            return result

    # Zip local file header. Empty/spanned archives use other signatures, which
    # carry no member names, so only the standard one is worth inspecting.
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(head)

    for signature in _SIGNATURES:
        marker, offset, label, mime, extensions = signature[:5]
        is_text = len(signature) > 5 and signature[5]
        if head[offset : offset + len(marker)] == marker:
            return _hit(label, mime, extensions, is_text=is_text)

    # OLE2 compound files (legacy .doc/.xls/.ppt/.msg) all share one signature
    # and telling them apart means walking the directory stream. MarkItDown's
    # extension-based guess is more reliable here, so defer to it.
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _UNKNOWN

    return _sniff_text(head)


class Magika:
    """Signature-based stand-in for ``magika.Magika``."""

    def __init__(self, *args, **kwargs):
        pass

    def identify_stream(self, stream: BinaryIO) -> MagikaResult:
        position = None
        try:
            position = stream.tell()
        except Exception:
            pass
        try:
            head = stream.read(_HEAD_BYTES)
        except Exception:
            return _UNKNOWN
        finally:
            if position is not None:
                try:
                    stream.seek(position)
                except Exception:
                    pass
        return _identify(head or b"")

    def identify_bytes(self, data: bytes) -> MagikaResult:
        return _identify(bytes(data[:_HEAD_BYTES]))

    def identify_path(self, path) -> MagikaResult:
        with open(path, "rb") as handle:
            return self.identify_stream(handle)
