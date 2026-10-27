"""The public landing page, llms.txt and the way back from the sign-in page."""

import re

from helpers import needs_db, running_app, sign_in

pytestmark = needs_db

PITCH = "Write LaTeX locally."


def test_signed_out_root_is_the_landing_page():
    with running_app() as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert PITCH in r.text
        assert 'href="/login"' in r.text
        # The share previews get the server's own address.
        assert "{{BASE_URL}}" not in r.text
        assert '<meta property="og:image" content="http://testserver/static/img/og-meerkat.jpg" />' in r.text


def test_landing_page_files_exist():
    """Every stylesheet, image, screenshot and video file the page names is
    served, and nothing comes from another site except links."""
    with running_app() as client:
        html = client.get("/").text
        paths = set(re.findall(r'(?:src|href|poster)="(/static/[^"]+)"', html))
        for srcset in re.findall(r'(?:srcset|imagesrcset)="([^"]+)"', html):
            paths.update(part.split()[0] for part in srcset.split(","))
        assert any("/screenshots/" in p for p in paths)
        assert any(p.endswith(".mp4") for p in paths)
        for path in sorted(paths):
            assert client.get(path).status_code == 200, path
        for tag in re.findall(r"<(?:script|link|img|source|video|iframe)\b[^>]*>", html):
            if 'rel="canonical"' not in tag:
                assert not re.search(r'(?:src|href|srcset|poster)="(?:https?:)?//', tag), tag


def test_tour_video_streams():
    """Safari plays a video only from a server that answers range requests."""
    with running_app() as client:
        r = client.get("/static/video/meertex-tour.mp4", headers={"Range": "bytes=0-1023"})
        assert r.status_code == 206
        assert r.headers["content-type"] == "video/mp4"
        assert len(r.content) == 1024


def test_llms_txt_is_plain_text():
    with running_app() as client:
        r = client.get("/llms.txt")
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.text.startswith("# meertex\n")


def test_signed_in_root_is_the_app():
    with running_app() as client:
        sign_in(client)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert '<script type="module" src="/static/js/app.js"></script>' in r.text
        assert PITCH not in r.text


def test_sign_in_page_links_to_the_landing_page():
    with running_app() as client:
        r = client.get("/login")
        assert r.status_code == 200
        assert '<a href="/">What is meertex?</a>' in r.text
