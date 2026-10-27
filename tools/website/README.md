# The landing page and its screenshots

Visitors who are not signed in get `app/static/landing.html` at `/`, with its
own stylesheet `app/static/css/landing.css`. `app/static/llms.txt` is served at
`/llms.txt`. `app/main.py` fills `{{BASE_URL}}` into the page's share previews
(OpenGraph, Twitter and the canonical address), because those must be absolute.
The page loads nothing from other sites and works without JavaScript. The small
inline script only opens the screenshots in a dialog instead of on their own,
and puts a play button over the tour video, which otherwise has its own
controls.

The screenshots in `app/static/img/screenshots/` are of the real app. `shots.py`
in this folder makes them. The tour video in `app/static/video/` is filmed from
the same demo account by `tools/video/` (its README says how).

## Taking the screenshots

`shots.py` starts its own server on port 8068 and the compile service with this
machine's TeX Live on port 8078. The server gets a temporary `DATA_DIR` and the
database `meertex_website`, which the script empties first. It then fills in a
made-up demo account, takes the screenshots with Google Chrome through
Playwright and stops everything again. A run takes about a minute.

It needs these things once:

1. The repository's virtualenv (`make venv`).
2. Postgres from `make infra` and the database of its own.

   ```sh
   docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_website"
   ```

3. latexmk, synctex and latexdiff on the `PATH`, and ImageMagick's `magick`.
4. A virtualenv with Playwright and httpx, and Google Chrome.

   ```sh
   python3 -m venv ~/.cache/meertex-pw
   ~/.cache/meertex-pw/bin/pip install playwright httpx
   ```

Then, from the repository's root:

```sh
~/.cache/meertex-pw/bin/python tools/website/shots.py            # all of them
~/.cache/meertex-pw/bin/python tools/website/shots.py editor     # only some
```

| Option | |
| --- | --- |
| `NAME ...` | Take only these screenshots. |
| `--out DIR` | Write the WebP files somewhere else, to compare before replacing. |
| `--raw DIR` | Keep the browser's PNGs too. |
| `--db URL` | Another database (`MEERTEX_WEBSITE_DB`). Its name must end in `_website`, because the script empties it. |
| `--port N`, `--compile-port N` | Other ports for the server and the compile service. |
| `--tmp DIR`, `--keep-tmp` | Where the server's data and logs go, and whether they stay. |
| `--headed` | Show the browser. |

Every screenshot is taken at twice the pixel density and saved as WebP twice,
1920 pixels wide (`name.webp`) and 960 pixels wide (`name-960.webp`). The page
picks one with `srcset`.

| Name | What it shows |
| --- | --- |
| `papers` | The paper list with folders, countdowns, outcomes and a waiting question. |
| `overview` | A paper's overview with its dates, a moved deadline, its versions and the question banner. |
| `timeline` | The timeline with writing windows, a dashed plan and a writing load that turns red at three. |
| `versions` | The Versions tab with the submission's compiled PDF. |
| `pdfdiff` | The Compare view with the latexdiff PDF of a submission and its revision. |
| `review` | A submitted review on the Conversation view, with an accepted suggestion and a resolved thread. |
| `review-files` | The review's Files view with an open thread next to the PDF. |
| `editor` | The editor with a coauthor's cursor, the `\cite` completion and the compiled PDF. |
| `references` | The References tab after a real check of the draft's `.bib`, which needs the network. Real works check out, one with a wrong year, and the made-up ones are in no index. |

## The demo account

Everything is made up, except five references of the green-llm paper, which
are real works so the References tab has entries that check out. The people
are Lena Vogt (signed in), Tomás Ibarra, Priya Raman and Jonas Keller, all on
example.com. Venue links point to
example.org. The dates are counted from the day of the run, so the countdowns
always look the same.

| Paper | Why it is there |
| --- | --- |
| `2027-eurosys-green-llm` | The editor and the review. Real LaTeX in `demo/green-llm/` (acmart, a `.bib`, sections), the submission due in two days. |
| `2027-icse-carbon-ci` | Handed in, with a compiled submission version and an unanswered rebuttal question. |
| `2027-e-energy-wasm-energy` | Accepted, camera-ready due in twelve days. |
| `2027-tosem-energy-repro` | A journal paper in revision. `demo/energy-repro/main.tex` is the submission, `revision/main.tex` the live text. |
| `2026-fse-green-metrics` and its fork `2027-esem-green-metrics` | Rejected, then resubmitted. |
| `2026-ict4s-energy-label` | Accepted and done. |
| `2027-ict4s` | A planned venue without a paper yet. Its writing window starts on the day of the run and overlaps three others on the timeline. |

## Adding a screenshot

Write a function in `shots.py` and mark it with `@shot("name")` (or
`@shot("name", width=1440, height=900)` for a larger viewport). It gets the run,
a page signed in as Lena, and the browser. It opens the page and waits until it
looks right. `run.url("green_llm", "/reviews")` builds a paper's address. Add
any data it needs to `Run.seed()`. Then add a `<figure class="shot">` to the
Screenshots section of `landing.html`, with a `srcset` like the others and an
alt text that says what the screenshot shows.

## Checking the page

```sh
MEERTEX_TEST_DB=postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_test_website \
  .venv/bin/pytest -q tests/test_landing.py
```

The tests check that a signed-out `/` is the landing page with absolute share
previews, that every file it names is served, that nothing loads from another
site, that `/llms.txt` is plain text, that a signed-in `/` is still the app and
that the sign-in page links back. They need their own database, made once like
the one above.
