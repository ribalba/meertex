# meertex-compile: TeX Live and the compile service (docs/DESIGN.md §14).
#
# texlive/texlive is the Island of TeX image. TL2025-historic is the full
# scheme of TeX Live 2025, frozen after its last update. The tag is rebuilt
# now and then, so the digest pins the exact image. About 2.6 GB to download
# and 5.5 GB on disk. It brings Python 3, latexmk, synctex, latexdiff, biber
# and the luaotfload font database. The service needs pandoc as well, which
# is added below (about 170 MB).
FROM texlive/texlive:TL2025-historic@sha256:dbd65d778eb83de9b45fb0a8deb3afe44cbbaba2272e4e1e644e94dda52c7109

ARG MEERTEX_VERSION=0.0.0
# pandoc turns a Markdown main file into LaTeX (docs/DESIGN.md §14,
# Markdown). Its static release build, pinned by version and checksum. The
# tests in CI use the same one (.github/workflows/tests.yml).
ARG PANDOC_VERSION=3.13
ARG TARGETARCH

LABEL org.opencontainers.image.title="meertex-compile" \
      org.opencontainers.image.description="Compiles the LaTeX and Markdown projects of meertex with TeX Live 2025 and pandoc, without shell escape." \
      org.opencontainers.image.source="https://github.com/ribalba/meertex" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${MEERTEX_VERSION}"

# HOME is on the tmpfs that compose mounts at /tmp. The font caches of LuaTeX
# and XeTeX go there, because the root file system is read-only.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/home \
    COMPILE_HOST=0.0.0.0 \
    COMPILE_PORT=8000 \
    COMPILE_JOBS_DIR=/jobs

# An unprivileged user of its own. /jobs is made here, owned by that user,
# because a fresh named volume copies the ownership of the directory it is
# mounted over.
RUN useradd --system --uid 10002 --user-group --home-dir /tmp/home --no-create-home compile \
 && mkdir -p /jobs \
 && chown compile:compile /jobs \
 && chmod 700 /jobs

# pandoc (PANDOC_VERSION above), checked against the release's checksum.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum=38aa33b325a2bc96a4c39409631212db55702f564250bcdbead180b3bf646389 ;; \
      arm64) sum=21e1d4ba09bd0b334590d204580fd4f6629b21be4ab6a32b26cc98982e9e0c68 ;; \
      *) echo "No pandoc for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/pandoc.tar.gz "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-${arch}.tar.gz"; \
    echo "$sum  /tmp/pandoc.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/pandoc.tar.gz -C /usr/local/bin --strip-components=2 "pandoc-${PANDOC_VERSION}/bin/pandoc"; \
    rm /tmp/pandoc.tar.gz; \
    pandoc --version | head -n 1

COPY service.py markdown.lua /srv/compile/

USER compile
WORKDIR /jobs
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# -I keeps the user site and the current directory off sys.path.
CMD ["python3", "-I", "/srv/compile/service.py"]
