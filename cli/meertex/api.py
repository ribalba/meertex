"""A thin client for the HTTP API (docs/DESIGN.md §11).

It only turns transport and HTTP failures into one exception type with the
server's ``detail``, so every command can report errors the same way. The
JSON shapes are passed through untouched. The commands print them with
``--json``, and the server stays the one place that defines them.
"""

from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

from . import __version__

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ApiError(Exception):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(self.message())

    def message(self) -> str:
        detail = self.detail
        if isinstance(detail, list):  # FastAPI's validation errors
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                    parts.append(f"{item.get('msg')} ({loc})" if loc else str(item.get("msg")))
                else:
                    parts.append(str(item))
            detail = ", ".join(parts)
        elif isinstance(detail, dict):
            detail = detail.get("message") or detail
        if self.status:
            return f"{detail} (HTTP {self.status})"
        return str(detail)


def project_path(ref: str) -> str:
    return "/api/projects/" + quote(ref, safe="")


def file_path(path: str) -> str:
    return quote(path, safe="/")


class Api:
    def __init__(self, server: str, token: str | None, transport: httpx.BaseTransport | None = None):
        self.server = server
        headers = {"User-Agent": f"meertex-cli/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=server, headers=headers, timeout=TIMEOUT, transport=transport)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, path: str, *, raw: bool = False, **kwargs) -> Any:
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"Cannot reach {self.server} ({exc})") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text.strip() or resp.reason_phrase
            raise ApiError(resp.status_code, detail)
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs) -> Any:
        return self.request("POST", path, json=json, **kwargs)

    def patch(self, path: str, json: Any = None, **kwargs) -> Any:
        return self.request("PATCH", path, json=json, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self.request("DELETE", path, **kwargs)

    def upload_pdf(self, ref: str, version: str, pdf: Path) -> Any:
        return self.put(
            f"{project_path(ref)}/versions/{quote(version, safe='')}/pdf",
            params={"name": pdf.name},
            content=pdf.read_bytes(),
            headers={"Content-Type": "application/pdf"},
        )
