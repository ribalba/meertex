# meertex-compile: TeX Live and the compile service (docs/DESIGN.md §14).
#
# texlive/texlive is the Island of TeX image. TL2025-historic is the full
# scheme of TeX Live 2025, frozen after its last update. The tag is rebuilt
# now and then, so the digest pins the exact image. About 2.6 GB to download
# and 5.5 GB on disk. It brings Python 3, latexmk, synctex, latexdiff, biber
# and the luaotfload font database, which is all the service needs.
FROM texlive/texlive:TL2025-historic@sha256:7056390eac37715fd3e01875037ceb4016cb17d44c611f7bbbfa7a012007979f

ARG MEERTEX_VERSION=0.0.0

LABEL org.opencontainers.image.title="meertex-compile" \
      org.opencontainers.image.description="Compiles the LaTeX projects of meertex with TeX Live 2025, without shell escape." \
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

COPY service.py /srv/compile/service.py

USER compile
WORKDIR /jobs
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# -I keeps the user site and the current directory off sys.path.
CMD ["python3", "-I", "/srv/compile/service.py"]
