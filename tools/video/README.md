# The tour video

The landing page (`app/static/landing.html`) shows a narrated tour of about two
minutes, filmed from the real app. `make_video.py` films it and puts it
together, `publish.py` turns it into the files of the page in
`app/static/video/`. It works like meerora's demo video (`docs/video` in
meerora), with the same voice.

Nothing in the video is drawn by hand except the end card, the pointer and the
frame of the terminal window. The rest is the running app:

- The server, the compile service and the made-up demo account of the
  screenshots (`tools/website/shots.py`), in a database of their own.
- Lena's checkout of the EuroSys paper with a real `meertex sync`, and vim and
  the meertex command in a real bash.
- Google Chrome, headed in kiosk mode on virtual screens (Xvfb), so nothing
  opens on your desktop. Coauthors who type elsewhere use a headless Chrome.
- The reference check against doi.org, Crossref, Semantic Scholar, DBLP and
  arXiv, which needs the network.

## Running it

It needs these things once:

1. The repository's virtualenv (`make venv`) and Postgres from `make infra`
   with the database of its own.

   ```sh
   docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_video_website"
   ```

2. latexmk, synctex and latexdiff, ImageMagick's `magick`, ffmpeg with
   libopenh264, libass, libsvtav1 and libopus, Xvfb, vim, Google Chrome and
   node.
3. xterm.js for the terminal window: `npm ci` in this folder.
4. A virtualenv with Playwright, httpx and Pillow, the same as the screenshots'
   plus Pillow.

   ```sh
   python3 -m venv ~/.cache/meertex-pw
   ~/.cache/meertex-pw/bin/pip install playwright httpx pillow
   ```

5. Kokoro for the voice: a virtualenv with kokoro-onnx, the system's
   espeak-ng, and `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the
   kokoro-onnx GitHub releases (model-files-v1.0) in `~/.cache/kokoro`.
   meerora's copy in `~/.cache/meerora/voice/kokoro` is found too.

   ```sh
   python3 -m venv ~/.cache/meertex-kokoro
   ~/.cache/meertex-kokoro/bin/pip install kokoro-onnx
   ```

Then, from the repository's root:

```sh
export MEERTEX_VIDEO_KOKORO_PYTHON=~/.cache/meertex-kokoro/bin/python
~/.cache/meertex-pw/bin/python tools/video/make_video.py           # writes ~/Videos/meertex-tour.mp4
~/.cache/meertex-pw/bin/python tools/video/publish.py ~/Videos/meertex-tour.mp4
```

A run films in real time and then cuts every frame, so it takes about ten
minutes, and `publish.py` two more. The work folder `~/.cache/meertex-video` keeps the
spoken sentences, one lossless clip per shot and a still from the middle of
every shot. It needs a few GB while it films, so it must not be on a tmpfs.

| Option | |
| --- | --- |
| `--only home,local` | Film only these shots and stop, to look at their clips and stills. |
| `--reuse` | Film only the shots that have no clip yet, then put the video together. |
| `--plan` | Speak the narration, print the storyboard with the times and stop. |
| `--out FILE` | Another file for the video. An existing file is never replaced, `-2` is added instead. |
| `--work DIR` | Another work folder. |
| `--keep-raw` | Keep the screen recordings in `WORK/raw`. |
| `--db URL`, `--port N`, `--compile-port N`, `--term-port N` | Another database (its name must end in `_website`, because the run empties it) and other ports (8069, 8079, 8089 and 8090). |

`publish.py` takes `--poster SHOT` for another poster, `--mp4-rate` and
`--no-webm`.

## The story

`narration.txt` holds the sentences, one scene per section, and how Kokoro
says meertex and LaTeX. `make_video.py` holds the shots. Each shot is one take:
`prepare()` sets the page up, the recorders start, and `act()` plays while
they run, timed by the shot's sentences. A shot is as long as its sentences
with 1.3 seconds between them, and never shorter than its hold.

| Scene | What the narration says | What the shots show |
| --- | --- | --- |
| intro | What meertex is. | The paper list with its countdowns. The pointer shows the deadline in the reader's time zone. |
| local | Write on your own computer, every save reaches coauthors within a second. | Lena types a sentence into vim in her checkout and saves. It appears in Tomás's editor in the browser. |
| browser | Coauthors edit the same document, autocomplete, the PDF compiles. | Tomás types in Lena's file, and his name shows over his cursor. Lena cites through the completion, and the PDF compiles. |
| refs | The reference check. | "Check references" on the ESEM paper finds a wrong year and a paper that does not exist (`demo/refs.bib`), and the row of the invented one opens. |
| venue | Venue, deadlines and time zones, the question after a deadline, the fixed version. | The ICSE paper's overview, "Yes" on the rebuttal question and its dialog, then the new version on the Versions tab. |
| compare | The PDF diff. | The journal paper's latexdiff PDF, scrolling. |
| review | Reviews like pull requests, comments on the PDF, how a comment was resolved. | Priya double-clicks the PDF, and the comment box opens at that line of the source. Then the conversation with how the caption changed, and she accepts it. |
| timeline | All papers and their writing time. | The timeline. |
| agents | The command line tool and the API, Claude. | `meertex projects` and `meertex threads` in a terminal. |
| ending | The line of the landing page, that meertex is part of the Meerverse, and meertex.com. | The end card over the paper list. "Part of the Meerverse" and the address join it with the sentence that says so, as on meerora's end card. |

`demo/refs.bib` has five real works with their real data and two wrong on
purpose. Before filming, Jonas checks the same file in a paper of his own, so
every lookup is in the server's cache and the check on camera takes a second.

## How it is made

- The browser lays pages out at 1536 by 864 CSS pixels and paints them at 5/3
  the density, 2560 by 1440. The video is 1920 by 1080, so the camera (a
  crop that eases between keyframes, `stage.Camera`) can come up to 4/3
  closer without enlarging anything. Pillow cuts the frames with sub-pixel
  precision, so slow moves do not shake.
- x11grab stamps the first frame with the wall clock, so each recording is cut
  at the moment its action started.
- The terminal is xterm.js on a screen of its own, fed by bash in a pseudo
  terminal (`terminal.html`, `stage.Terminal`). Its recording is laid over
  the browser's with rounded corners and a shadow. vim gets a narrow one over
  the editor's PDF, the command line a wide one.
- Playwright's mouse never moves the X pointer, so the page draws a pointer
  that follows the mouse events and shows a ring on a click. It sits in the
  top layer, above dialogs.
- The name over a remote cursor shows on hover in the app, so Lena's pointer
  moves onto Tomás's cursor.
- The narration is Kokoro v1.0 (82M, Apache 2.0), voice `af_heart` at speed
  0.95, one WAV file per sentence. A sentence keeps its recording while its
  text stays the same.
- Under the voice runs a quiet pad, a D major chord of sine tones with a
  little brown noise. The sound is normalised to about -17 LUFS.
- Subtitles are burned in with libass, white Noto Sans on a panel of the
  app's text colour. Shots whose action is at the bottom put them at the top.
  The ending has none, the end card carries the text.
- whisper (ggml base) reads every sentence back as written. It hears meertex
  as "Mirtech", which is the intended MEER-tek.
- The clips are lossless (FFV1). The video is H.264 at 12 Mbit/s with AAC,
  `publish.py` makes the page's smaller AV1 and H.264 versions from it.
