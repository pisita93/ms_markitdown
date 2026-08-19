"""
A stub for ``pypdfium2``, for WebAssembly (Pyodide).

``pdfplumber`` imports ``pypdfium2`` at module load to build its ``PageImage``
rendering helper. pypdfium2 wraps the native PDFium library and has no
WebAssembly build.

MarkItDown's PDF converter only calls ``extract_text``/``extract_words`` and never
renders a page to an image, so the rendering path is unreachable here. This stub
satisfies the import and raises a clear error if anything ever does try to
render.
"""

__version__ = "4.30.0"

V_PYPDFIUM2 = __version__
V_LIBPDFIUM = "unavailable"


class PdfiumError(RuntimeError):
    pass


def _unavailable(name):
    def _raise(*args, **kwargs):
        raise PdfiumError(
            f"pypdfium2.{name} is unavailable in WebAssembly builds. PDF page "
            "rendering is not supported here; text and table extraction are."
        )

    return _raise


class PdfDocument:
    def __init__(self, *args, **kwargs):
        _unavailable("PdfDocument")()


class PdfPage:
    def __init__(self, *args, **kwargs):
        _unavailable("PdfPage")()


class PdfBitmap:
    def __init__(self, *args, **kwargs):
        _unavailable("PdfBitmap")()


PdfMatrix = _unavailable("PdfMatrix")
PdfTextPage = _unavailable("PdfTextPage")
raw = None
