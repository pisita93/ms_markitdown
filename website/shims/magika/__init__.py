"""
A drop-in replacement for the ``magika`` package, for WebAssembly (Pyodide).

MarkItDown imports ``magika`` unconditionally and uses it to identify a stream's
type from its content. The real package infers types with an ONNX neural network,
and ``onnxruntime`` has no WebAssembly build, so it cannot be installed in
Pyodide.

This module keeps the same import surface and result shape, but identifies types
with magic-byte signatures instead. It is deliberately conservative: when a
signature does not match confidently it reports ``status == "error"``, which is
the same path MarkItDown takes when the real Magika is unsure. MarkItDown then
falls back to the filename extension and MIME type, which the browser always
supplies for uploaded files.
"""

from ._sniff import Magika, MagikaResult

__all__ = ["Magika", "MagikaResult"]
__version__ = "0.6.1"
