# Build from the repository root, so the local markitdown source is available:
#
#     docker build -f packages/markitdown-mcp-remote/Dockerfile -t markitdown-mcp-remote .
#
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXIFTOOL_PATH=/usr/bin/exiftool \
    FFMPEG_PATH=/usr/bin/ffmpeg

# MarkItDown shells out to these for media metadata and audio. They are not
# needed for documents, but including them means an image or audio URL degrades
# to "no text found" rather than a missing-dependency error.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg exiftool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/markitdown /app/packages/markitdown
COPY packages/markitdown-mcp-remote /app/packages/markitdown-mcp-remote

RUN pip install --no-cache-dir \
    /app/packages/markitdown[all] \
    /app/packages/markitdown-mcp-remote

# Run unprivileged: this process fetches attacker-influenced URLs, so it should
# not also hold root inside the container.
RUN useradd --create-home --uid 10001 markitdown
USER markitdown

EXPOSE 8080

# MARKITDOWN_MCP_PUBLIC_URL and MARKITDOWN_MCP_PASSPHRASE must be supplied at
# run time; the server refuses to start without them.
CMD ["markitdown-mcp-remote"]
