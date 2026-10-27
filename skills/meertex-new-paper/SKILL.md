---
name: meertex-new-paper
description: Start a new paper in meertex from a conference or call-for-papers URL. Reads the CFP, extracts the venue's fields and every deadline with its time zone, creates the meertex project through the CLI and checks it out into ~/papers/<name>. Use when the user says "new paper for <conference>", "start a project for this CFP", pastes a conference URL and wants to write for it, or wants to resubmit a paper to another venue.
---

# Start a new paper from a conference URL

meertex keeps every paper as a project with the venue's metadata, its deadlines
and fixed versions. The `meertex` CLI talks to the server. This skill turns a
conference URL into a project and a local checkout.

## Before you start

- `meertex status --json` must work. If it says the CLI is not logged in, ask
  the user to run `meertex login` (it mails a code). Never ask for the code
  yourself and never print the token.
- If the user wants to **resubmit** an existing paper to a new venue, do not
  create a new project. Use `meertex fork <old project> --from-json <file>`
  with the new venue's fields (same JSON as below). It keeps the files and links
  the two projects.
- If the paper already exists as a folder in `~/papers` that is not a meertex
  checkout yet, create the project with `meertex link <dir>` instead of
  `--checkout`, after the same extraction.
- If the user knows the venue but not the topic yet ("I want to submit
  something to ICT4S"), make a **plan**: the same extraction, then
  `meertex new --from-json <file> --planned` without a title, without `--zip`
  and without `--checkout`. It shows on the timeline. Later
  `meertex start <plan> --title "..."` turns it into a paper. If a plan for
  that venue and year exists already, start it instead of making a new project.

## 1. Read the call for papers

Fetch the URL the user gave. Deadlines are often not on that page. Follow the
links that matter and read them too:

- "Call for papers" or the track page (research track, industry track, ...)
- "Important dates" or "Dates"
- "Submission", "Author instructions", "Formatting" (page limit, template)
- The submission system link (HotCRP, EasyChair, OpenReview, CMT)

If the venue has several tracks or several submission cycles and the user did
not say which one, ask before you go on. Dates differ per track and cycle.

## 2. Extract these fields

Fill in what the CFP states. Leave a field out when it does not say. Never guess
a date.

| Field | What to put there | Example |
| --- | --- | --- |
| `conference` | The acronym as the venue writes it | `ICSE`, `EuroSys`, `e-Energy` |
| `conference_name` | The full name, with the edition if given | `48th IEEE/ACM International Conference on Software Engineering` |
| `year` | The year the conference takes place, not the deadline's year | `2027` |
| `track` | The track this paper goes to | `Research Track` |
| `cfp_url` | The URL of the call for papers | |
| `conference_url` | The conference homepage | |
| `submission_url` | The submission system for this track | `https://icse2027.hotcrp.com` |
| `location` | City and country | `Rio de Janeiro, Brazil` |
| `publisher` | Who publishes the proceedings | `ACM`, `IEEE`, `Springer LNCS`, `LIPIcs`, `USENIX` |
| `page_limit` | The limit as the CFP words it, short | `10 pages + 2 pages of references` |
| `template` | The LaTeX class and option the CFP requires | `acmart sigconf`, `IEEEtran conference`, `llncs` |
| `review_model` | `double-blind`, `single-blind` or `open` | |
| `notes` | Rules that bite at the deadline, as short Markdown bullets | anonymisation rules, mandatory abstract registration, artifact evaluation, "no extensions", ORCID, data availability statement |

### Dates

Each deadline becomes one entry in `dates`.

| `kind` | Use it for |
| --- | --- |
| `abstract` | Abstract or paper registration |
| `submission` | Full paper deadline |
| `supplementary` | Supplementary material, appendix, artifact deadline that goes with the submission |
| `rebuttal_start`, `rebuttal_end` | The author response period |
| `notification` | Acceptance notification |
| `revision` | A major-revision or shepherding deadline |
| `camera_ready` | Camera-ready deadline |
| `conference_start`, `conference_end` | The conference itself |
| `other` | Anything else worth a countdown, with a `label` (artifact evaluation, registration deadline for authors) |

Time and time zone, in this order of preference:

- With a time: `{"kind": "submission", "due_at": "2027-01-15T23:59", "tz": "AoE"}`.
- Day only: `{"kind": "notification", "date": "2027-03-01", "tz": "AoE"}`. meertex
  treats it as the end of that day.
- `tz` is `AoE`, `UTC`, an offset like `UTC+2`, or an IANA zone. Write zones
  as IANA names: Pacific time is `America/Los_Angeles`, CET/CEST is
  `Europe/Berlin`, Eastern is `America/New_York`.
- When the CFP gives no time zone, use `AoE` and say so in your summary.
- When a date was extended (the old one struck through), use the new one and
  put the old one in `notes`.
- A date marked TBA or TBD is left out. Mention it in your summary.

## 3. Ask only for what the CFP cannot tell you

- **Title.** A working title is fine. Use what the user said if they said one.
- **Short name** (`paper`) for the project name. Propose two or three words in
  lowercase with hyphens (`green-llm`, `energy-ids`) and take it unless the
  user objects. The project name is built from the user's naming pattern,
  by default `<year>-<conference>-<paper>`, e.g. `2027-icse-green-llm`.
- **Folder and labels**, only if the user mentioned them (`--folder 2027`,
  `"labels": ["green-software"]`).

## 4. Check for duplicates

```bash
meertex projects --json --archived | jq '.[] | select(.conference | ascii_downcase == "icse") | {name, year, title}'
```

If a project for the same conference and year with a similar title exists, ask
the user before you create another one.

## 5. Create and check out

Write the body to a JSON file in your scratchpad, then look at the name it
would get, then create it.

```json
{
  "title": "Measuring the Energy of LLM Inference",
  "paper": "green-llm",
  "conference": "ICSE",
  "conference_name": "48th IEEE/ACM International Conference on Software Engineering",
  "year": 2027,
  "track": "Research Track",
  "cfp_url": "https://conf.researchr.org/track/icse-2027/icse-2027-research-track",
  "conference_url": "https://conf.researchr.org/home/icse-2027",
  "submission_url": "https://icse2027.hotcrp.com",
  "location": "Rio de Janeiro, Brazil",
  "publisher": "IEEE",
  "page_limit": "10 pages + 2 pages of references",
  "template": "IEEEtran conference",
  "review_model": "double-blind",
  "notes": "- Double-blind: no author names, cite own work in the third person\n- Data availability statement required",
  "dates": [
    {"kind": "abstract", "due_at": "2026-07-24T23:59", "tz": "AoE"},
    {"kind": "submission", "due_at": "2026-07-31T23:59", "tz": "AoE"},
    {"kind": "rebuttal_start", "date": "2026-10-28", "tz": "AoE"},
    {"kind": "rebuttal_end", "date": "2026-10-31", "tz": "AoE"},
    {"kind": "notification", "date": "2026-11-27", "tz": "AoE"},
    {"kind": "camera_ready", "date": "2027-01-23", "tz": "AoE"},
    {"kind": "conference_start", "date": "2027-04-25", "tz": "America/Sao_Paulo"},
    {"kind": "conference_end", "date": "2027-05-01", "tz": "America/Sao_Paulo"}
  ]
}
```

If the CFP links the venue's official LaTeX author kit as a zip (ACM's
acmart, IEEE's conference template, Springer's LNCS, LIPIcs, USENIX and so
on), download it into your scratchpad and pass it with `--zip`. Its files
become the paper's files, and meertex picks the kit's sample paper as the
main file. Take the zip from the publisher's or the venue's own site, never
from a mirror. Leave `--zip` out when there is no official zip.

```bash
meertex new --from-json "$SCRATCH/new-paper.json" --dry-run
meertex new --from-json "$SCRATCH/new-paper.json" --zip "$SCRATCH/author-kit.zip" --checkout --json
```

`--checkout` puts it into `~/papers/<name>`. The JSON result has the project,
`checkout_dir` and, with `--zip`, `imported` (the files, the main file and
`main_candidates`). A kit often holds several sample papers (one per format).
When `main_candidates` has more than one, tell the user which one became the
main file and that `meertex set <name> main_file=<file>` changes it. If the
import fails, nothing is created, and the error says why.

## 6. Without an author kit (only when the user asks)

When the venue has no zip and the user wants a starting point, set up a
minimal `main.tex` in the checkout with the required class and the title.
Take the class from TeX Live when it is installed (`kpsewhich acmart.cls`).
Do not copy sample content beyond what the class needs.

Files written into the checkout reach the server through the sync client. If
`meertex status` does not show the sync service running, run
`meertex sync --once` in the checkout afterwards.

## 7. Report back

Tell the user, in a few lines:

- the project name, the checkout path and the URL,
- the deadlines with their countdowns (`meertex show <name>` prints them),
- what you could not find or are unsure of (missing time zone, TBA dates, a
  track you picked).
