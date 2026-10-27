# meertex design

The contract every part of meertex codes against. It covers accounts, folders,
projects and their dates, versions, the live document and its sync protocol,
the HTTP API, the CLI, the deployment and the reference check. If code and
this file disagree, one of them is a bug.

meertex is a hosted LaTeX workspace for papers. You edit locally (VS Code,
Claude), a small sync client keeps the folder in step with the server within
a second, and coauthors see and edit the current state. Every paper carries
its conference, its deadlines and its fixed versions (submission, rebuttal,
revisions, camera-ready).

It is built in three phases.

| Phase | Contents |
| --- | --- |
| 1 | Accounts, folders, projects, dates and their questions, versions, the live document with git history, the sync protocol, the token API, the web app for all of that, the CLI with the sync client, the new-paper skill. |
| 2a | The browser editor (§10, The browser editor). Built. |
| 2b | The compile service and the PDF view, latexdiff between versions (§14). |
| 3 | GitHub-style reviews (§13). |

Phases 2b and 3 are being built.

Sections marked *(phase 2b)* or *(phase 3)* describe what is planned and are
not built yet.

## 1. Stack and conventions

meerpad's stack and habits, so the apps look and run alike.

- FastAPI, SQLAlchemy 2, PostgreSQL 18, pydantic-settings. One uvicorn
  process with one worker, because the live documents (§8) are held in
  memory. Coolify runs one container of it.
- The web app is static HTML, CSS and plain JavaScript modules under
  `app/static`, with no build step.
- Naive UTC datetimes everywhere inside (`models.utcnow()`), ISO 8601 with
  `Z` at the API boundary.
- Ids are UUID4 strings.
- Schema changes go through `app/migrations.py` (meerpad's mechanism).
- Tests run against a real Postgres (`MEERTEX_TEST_DB`, `make test-db`).
- Ports are 8060 for the app and 5436 for the laptop database. meerail has
  8000/5432, meercal 8010/5433, meerink 8030, meerpic 8040/5434 and meerpad
  8050/5435.
- Files on the server live under `DATA_DIR` (`/data` in the container).
  `DATA_DIR/projects/<project id>/` holds `repo/` (the git history, §9) and
  `blobs/` (binary files by SHA-256, §8).

## 2. Accounts and sharing

### Accounts

meerpad's scheme, carried over unchanged. You enter your email and get a
link and a six-character code, and either one signs you in. There are no
passwords. A brand-new email gets an account on its first sign-in.

A user has `id, email, name, timezone, api_token, naming_pattern, writing_days, reminders, reminder_days, reminder_hour, created_at`.
`naming_pattern` is described in §4, `writing_days` and the reminders in §5.

The API token is for scripts, the CLI and agents. It is sent as
`Authorization: Bearer <token>` or, where a header is impossible, as
`?token=`.

### Members and roles

A project has members. Each member has one role.

| Role | Can |
| --- | --- |
| `owner` | Everything, including deleting the project and managing members. There is exactly one owner. |
| `editor` | Edit files and metadata, answer questions, create versions, invite reviewers and viewers. |
| `reviewer` | Read everything, and review (phase 3). |
| `viewer` | Read everything. |

Inviting is by email (`POST /api/projects/{ref}/members {email, role}`). An
email without an account gets one right away, so the membership exists
before the person ever signs in. The invite is mailed with a sign-in link
that lands on the project.

A project that is not yours and where you are not a member is 404
everywhere, never 403. Too low a role for a write is 403.

## 3. Folders and the project list

Folders are personal. Each user arranges the projects they can see in their
own tree, so a shared paper can sit in `2027/Accepted` for you and in
`Collaborations` for your coauthor.

- A **folder** has `id, user_id, parent_id, name, position, created_at`.
  `parent_id = null` is the top level. Names are unique among siblings,
  ignoring case, so a folder can be addressed by its path (`2027/Submitted`).
  `position` is a float for ordering among siblings.
- Where a project sits is stored on the member row, `memberships.folder_id`
  and `memberships.position`. `folder_id = null` is the top level.
- Deleting a folder that still holds folders or projects is 409. The client
  moves things out first.
- Moving a folder under its own descendant is 422.

The project list shows, per project, the name, title, conference and year,
the outcome badge, labels, the next date with a countdown (§5) and a marker
when a question is waiting (§6). It filters by folder, outcome, label,
archived and free text.

### The timeline

A Gantt chart of all your papers, at `/timeline` (a tab next to the paper
list), to see what to do when.

- `GET /api/timeline?archived=false` returns
  `{writing_days, today, projects: [summary + {dates: [date]}]}`, ordered by
  writing start, then by next date.
- One row per project, the name on the left. On the time axis:
  - the writing window as a bar ending at the hand-in date,
  - the review time (hand-in to notification), the rebuttal window, the time
    from acceptance to camera-ready, and the conference itself, each as a
    lighter bar,
  - every date as a marker with its label, the date in the venue's time zone
    and the countdown on hover,
  - a line for today. What has passed is dimmed. Planned projects are drawn
    dashed.
- Above the rows, a **writing load** strip counts how many writing windows
  overlap on each day, amber from two and red from three, with the papers on
  hover.
- At the top of the page: "I need **N** days to write a paper" (the user's
  `writing_days`), the zoom (weeks, months, quarters), the folder filter and
  "show finished papers". A row's menu sets that paper's own writing time.
- Rows link to the papers. On phones the chart scrolls sideways under a fixed
  name column.
- `meertex timeline [--writing-days N]` prints the same as text: when to
  start writing what, what is due, and where windows overlap.

### Sharing the timeline

A public, read-only link to the timeline, for supervisors and others without
an account. They see the chart and nothing else of meertex.

- One link per user, in `timeline_shares {user_id, token, folder_id, created_at}`.
  The token is 32 random bytes, URL-safe.
- The link is `<BASE_URL>/timeline/<token>`. The page needs no session. It
  shows the chart with its zoom and "show finished papers", read-only. There
  is no "I need N days", no folder filter, no row menu, no link into a paper
  and no search or menu in the header. The page is sent with
  `X-Robots-Tag: noindex, nofollow` and `Referrer-Policy: no-referrer`.
- It shows the papers of your timeline that are not archived. With a
  `folder_id` it shows only the ones you put into that folder or below it.
  Deleting that folder deletes the link, so a link never widens to every
  paper.
- Of a paper it sends `{id, name, title, conference, year, track, outcome,
  planned, writing_days, writing, next_date, dates}`. Of a date it sends
  `{id, kind, label, due_at, tz, date_only, local, passed, seconds_left,
  answer, changes: [{at, from, to}]}`. Labels, folders, roles, questions,
  notes, people and files stay out, and so does who moved a date.
- `GET /api/timeline/share` returns `{token, url, folder_id, folder,
  created_at}` or null. `folder` is the folder's path, null for every paper.
- `POST /api/timeline/share {folder?}` makes a new link and returns 201.
  `folder` is an id or a path, null or left out for every paper. The link you
  had before stops working.
- `PATCH /api/timeline/share {folder}` changes which papers the link shows.
  The link stays the same. 404 when you share none.
- `DELETE /api/timeline/share` stops sharing.
- `GET /api/shared/timeline/{token}` needs no session. It returns
  `{owner, writing_days, today, projects}`, where `owner` is your name or
  null. A link that was replaced or stopped is 404.
- On `/timeline` the Share button opens a dialog with the folder choice, the
  link with a copy button, "New link" and "Stop sharing". The button reads
  "Shared" while a link works.

## 4. Projects

### Fields

| Field | Type | Notes |
| --- | --- | --- |
| `id` | uuid | |
| `name` | string | The fixed name from the naming pattern, below. Unique per owner. It is the checkout folder's name. |
| `title` | string | The paper's title. |
| `paper` | string | Short name of the paper, the `{paper}` in the pattern (`green-llm`). Derived from the title when empty. |
| `conference` | string | Acronym as the venue writes it (`ICSE`, `EuroSys`). |
| `conference_name` | string | Full name. |
| `year` | int | The year of the conference, not of the deadline. |
| `track` | string | `Research Track`, `Industry`, `Short papers`. |
| `cfp_url` | string | The call for papers. |
| `conference_url` | string | The conference homepage. |
| `submission_url` | string | HotCRP, EasyChair and so on. |
| `location` | string | City and country. |
| `publisher` | string | ACM, IEEE, Springer, LIPIcs, USENIX. |
| `page_limit` | string | Free text as the CFP says it (`10 pages + 2 for references`). |
| `template` | string | `acmart sigconf`, `IEEEtran conference`, `llncs`. |
| `review_model` | string | `double-blind`, `single-blind`, `open` or empty. |
| `notes` | text | Markdown, anything else from the CFP or our own. |
| `main_file` | string | Default `main.tex`. The compile entry point (phase 2b) and the base of the ignored output PDF (§10). |
| `outcome` | enum or null | `accepted`, `rejected`, `revision`, `withdrawn`, `not_submitted`. Null while nothing is decided. |
| `outcome_at` | datetime | When the outcome was set. |
| `labels` | list of strings | Free labels (`green-software`, `with-arne`). |
| `archived` | bool | Hidden from the default list. |
| `doi`, `publication_url` | string | Filled in after acceptance. |
| `forked_from_id` | uuid or null | The project this one was resubmitted from (§4, Resubmission). |
| `owner_id`, `created_at`, `updated_at` | | |

Strings default to empty, never null, except where the table says null.

### Naming

The name is built once, when the project is created, from the owner's
`naming_pattern`. The default is `{year}-{conference|lower}-{paper|lower}`,
which gives `2027-icse-green-llm`.

- Placeholders are `{year}`, `{conference}`, `{paper}`, `{track}` and
  `{title}`. Each value is turned into a slug first. German umlauts become
  `ae oe ue ss`, other accents are dropped, and every run of characters
  outside `A-Z a-z 0-9` becomes one `-`, trimmed at both ends.
- A placeholder can take the filter `|lower` or `|upper`.
- Text outside placeholders is kept as written (`{conference}{year}-{paper}`
  gives `EuroSys2027-pacer`).
- Empty placeholders collapse with their separator, so a missing track does
  not leave `--` behind.
- When `paper` is empty it is derived from the title. The first three words
  that are not stop words (`a an the of for on in and to with towards via
  using`) are slugged and lowercased.
- If the name is taken among the owner's projects, `-2`, `-3` and so on are
  appended.
- A valid name matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$`.
- The name does not follow later changes to the fields. Renaming is an
  explicit `PATCH {name}`. Checkouts keep their folder and the CLI points
  the rename out.

`PATCH /api/auth/me {naming_pattern}` changes the pattern. A pattern without
any placeholder is 422. `POST /api/projects` with `dry_run: true` returns the
name a project would get, without creating anything.

### Starting files

A new project starts empty, with the `basic` starter (a `main.tex` and a
`refs.bib`), or from a **template zip**, such as a publisher's author kit.
`POST /api/projects/{ref}/import` takes the zip as the body and writes its
files into the live document as one snapshot.

- A folder that holds everything (`acmart-primary/...`) is stripped, as often
  as there is one.
- Left out: `__MACOSX`, `.git`, `._*` and similar leftovers, LaTeX build
  outputs, links, encrypted members and names that lead outside the zip.
  Nothing is unpacked to disk.
- At most 50 MB of zip (`MAX_BLOB_BYTES`), 2000 files and 200 MB unpacked.
- Files that exist already are a 422, unless `?replace=true`.
- The main file becomes the zip's most likely paper when the project's own
  `main_file` is not among its files: a `.tex` with `\documentclass` and
  `\begin{document}`, fewest folders first, then usual names (`main.tex`,
  `paper.tex`), and never a class's own manual when a sample is there.
- Returns `{files, skipped: [{path, reason}], main_file, main_file_changed, main_candidates}`.
- The new-paper form offers it as "A publisher's template". When the import
  fails, the paper it just made is deleted again, so a failed upload leaves
  nothing behind.

### Planned papers

A **planned** project is a venue you mean to hand something in to before the
paper's topic is clear ("something for ICT4S 2027"). It has the venue's
fields and dates, and usually no title and no files yet.

- `planned: bool` on the project, default false. `POST /api/projects` with
  `planned: true` needs no title. The name comes from the naming pattern as
  always, so with an empty `{paper}` it is `2027-ict4s`.
- A planned project asks no questions (§6). It has a next date and shows in
  the timeline like any other.
- `POST /api/projects/{ref}/start {title, paper?, starter?, keep_name?}`
  turns a plan into a paper (editors and the owner). It sets `planned` to
  false and the title, builds the name again from the pattern (unless
  `keep_name`), and writes the starter files when the project has none.
  The new name is unique among the owner's projects like any other.
- `GET /api/projects?planned=true|false` filters. Without it both come.
- The list, the timeline and the overview mark planned projects, and the
  overview offers "Start the paper".

### Resubmission

A rejected paper usually goes to another venue. `POST /api/projects/{ref}/fork`
creates a new project with the new venue's fields and dates. It starts from
the old project's live files, gets its own name, and its `forked_from_id`
points back. The old project keeps its outcome, so "all my rejected papers"
stays true. The new project's git history starts with one commit that says
where it came from.

## 5. Dates

A project has any number of dates. A date has
`id, project_id, kind, label, due_at, tz, date_only, answer, answered_at, answered_by, changes, position, created_at`.

| `kind` | Default label | Question when it has passed (§6) |
| --- | --- | --- |
| `abstract` | Abstract registration | Did you register the abstract? |
| `submission` | Paper submission | Did you hand it in? |
| `supplementary` | Supplementary material | Did you hand in the supplementary material? |
| `rebuttal_start` | Rebuttal opens | none |
| `rebuttal_end` | Rebuttal deadline | Did you send a rebuttal? |
| `notification` | Notification | Was the paper accepted? |
| `revision` | Revision due | Did you hand in the revision? |
| `camera_ready` | Camera-ready | Did you hand in the camera-ready version? |
| `conference_start` | Conference starts | none |
| `conference_end` | Conference ends | none |
| `other` | (label required) | Is it done? |

### Time and time zones

- `due_at` is stored as a UTC instant.
- `tz` is how the venue states it. It is `AoE` (Anywhere on Earth, UTC-12),
  `UTC`, a fixed offset like `UTC+2`, or an IANA zone like `Europe/Berlin`.
  Default `AoE`, because most CFPs use it.
- `date_only = true` means the venue gave a day without a time. It is stored
  as 23:59:59 of that day in `tz` and shown as the day.
- Input takes either `due_at` (ISO 8601, with an offset, or local to `tz`
  without one) or `date` (`YYYY-MM-DD`, which sets `date_only`). Output gives
  `due_at` (UTC, `Z`), `tz`, `date_only` and `local` (the same instant as
  ISO 8601 in `tz`).

### Moving a date

Deadlines move all the time. Changing `due_at` appends
`{at, by, from, to}` to `changes`, clears `answer`, and the web app shows
"moved from 15 Jan" next to the date.

### Writing time

How long it takes to write a paper decides when to start.

- `writing_days` on the user (default 30, 1 to 365) is the usual time, and
  `writing_days` on a project (null by default) overrides it for that paper.
  `PATCH /api/auth/me {writing_days}` and `PATCH /api/projects/{ref} {writing_days}`
  set them (null on the project goes back to the usual time).
- A project's **writing window** ends at the date the paper has to be handed
  in next: the `submission` date while the paper is not handed in, the
  `revision` date while the outcome is `revision`. It starts `writing_days`
  days earlier. A project without such a date, or whose outcome is decided
  otherwise, has none.
- The summary (§11) carries `planned`, `writing_days` (the one that counts),
  `own_writing_days` (the project's own, or null) and
  `writing: {days, own, start, end, date_id} | null`. `own` says the
  project overrides the usual time. `start` and `end` are UTC instants.

### The next date and the countdown

The list and the project page show the **next date**. It is the earliest
date with `due_at >= now` and no answer, skipping dates that cannot matter
any more.

- After `outcome` is `rejected`, `withdrawn` or `not_submitted`, no date is
  next.
- `camera_ready` counts only once the outcome is `accepted`.
- `revision` counts only once the outcome is `revision`.

It goes out as `next_date: {id, kind, label, due_at, tz, date_only, local, seconds_left}`.
The web app writes it as "Submission in 12 days" and colours it red under
3 days and amber under 14 days.

### Reminders

meertex mails each person one digest a day before their deadlines, never one
mail per date, and only when there is something to say. The code is in
`app/reminders.py`.

- **Who.** The owner and the editors of a paper, each by their own settings.
  Reviewers and viewers get nothing. Archived papers never remind. Anyone can
  mute one paper for themselves (`memberships.reminders_muted`).
- **What.** Per paper, sorted by date:
  - every date still ahead that the next date would consider (above) and that
    something is handed in for (`abstract`, `submission`, `supplementary`,
    `rebuttal_end`, `revision`, `camera_ready`, `other`), once it comes within
    one of the person's reminder days,
  - "start writing" when the writing window opens within a day,
  - the questions (§6) whose date passed since the last digest.

  Plans are included and marked as plans. Dates are written in the venue's
  zone, countdowns in days, and every paper links to `<BASE_URL>/p/<name>`.
- **Days** are counted in the venue's calendar. A deadline on 15 Jan AoE is
  "today" while it is 15 Jan anywhere on earth, although it ends on 16 Jan in
  UTC. A date reminds when the smallest reminder day it is within was not
  told yet. The larger ones are recorded with it, so a date added two days
  before its deadline reminds once.
- **Settings** on the user: `reminders` (on or off, default on),
  `reminder_days` (days before a date, 0 is the day itself, default
  `[14, 3, 1, 0]`, at most 10 values from 0 to 365, stored largest first) and
  `reminder_hour` (0 to 23 in the user's `timezone`, default 8).
  `PATCH /api/auth/me` sets them. The web app has them under Settings,
  Reminders, and the mute button in the paper's sidebar.
- **Sending.** A loop in the app's lifespan wakes every five minutes and works
  in a thread. It puts together the digest of everyone whose hour has come in
  their own zone and who had none yet on their own today. Each person is a
  transaction of its own with their user row locked
  (`FOR NO KEY UPDATE SKIP LOCKED`), so one failing mail never stops the
  others and a second copy of the app skips the person. A failed mail
  commits nothing and is tried again on the next round.
- **Nothing twice.** Every item a digest told is a row in `reminders_sent`,
  one per `(user_id, date_id, kind, threshold, due_at)`. `kind` is `date`
  (threshold in days), `writing` (threshold the writing days, `due_at` when
  writing starts) or `question` (threshold 0). Restarts do not resend, and a
  date that moves has a new `due_at` and reminds again. `users.reminded_at`
  says when the last digest was put together, whether it said anything or
  not. Rows about dates more than 90 days gone are deleted.
- `REMINDERS_ENABLED=false` turns the loop off, for a laptop or a second copy
  of the app on the same database. Without `SMTP_HOST` the digests go to the
  log like every other mail.
- `GET /api/reminders/preview` returns what the next digest would say,
  whatever the hour and whatever earlier digests said. So it lists every date
  within the reminder days and every waiting question.
  `meertex reminders` prints it.

A digest reads like this.

```text
Subject: meertex · Paper submission in 3 days for 2027-icse-green-llm and 2 more

Hi,

Here is what is coming up in your papers.

Towards Green Large Language Models (ICSE 2027)
- The abstract registration deadline passed on 8 Jan 2027. Did you register the abstract?
- Paper submission in 3 days, 15 Jan 2027 (AoE).
https://meertex.com/p/2027-icse-green-llm

ICT4S 2027, still a plan
- Paper submission in 14 days, 26 Jan 2027 (AoE).
https://meertex.com/p/2027-ict4s

Every paper and its writing time is on your timeline at https://meertex.com/timeline.

This mail comes at most once a day, at 08:00 Europe/Berlin, when there is
something to say. Change when or turn it off at
https://meertex.com/settings#reminders. A paper's own page can mute it.
```

## 6. Questions

When a date with a question has passed and has no answer, the project has a
question. The web app shows it as a banner on the project and a marker in
the list. The CLI and the API list them too. Questions are computed, never
stored. Answering stores `answer` on the date.

A question is only asked when it still makes sense.

- `rebuttal_end`, `notification` and `revision` are only asked once the
  paper was handed in. That is the case when the `submission` date was
  answered `yes`, or a `submission` version exists, or the project has no
  `submission` date.
- `notification` is only asked while `outcome` is null.
- `camera_ready` is only asked when `outcome` is `accepted`.
- `abstract`, `submission`, `supplementary` and `rebuttal_end` are no longer
  asked once there is an outcome.
- `revision` is only asked when `outcome` is `revision`.
- Nothing is asked after `outcome` is `rejected`, `withdrawn` or
  `not_submitted`.

A question goes out as
`{project_id, project_name, date_id, kind, label, due_at, text, answers}`.
`POST /api/projects/{ref}/dates/{id}/answer {answer, ...}` answers it.

| kind | answers | What each one does |
| --- | --- | --- |
| `submission` | `yes`, `no`, `moved` | `yes` creates the submission version (§7) and takes `version: {at, note}`. `no` sets outcome `not_submitted`. `moved` takes a new `due_at` or `date` and moves the date (§5). |
| `abstract`, `supplementary`, `other` | `yes`, `no`, `moved` | Recorded. `no` on `abstract` sets outcome `not_submitted`. |
| `rebuttal_end` | `yes`, `no`, `moved` | `yes` creates a rebuttal version. |
| `notification` | `accepted`, `rejected`, `revision`, `moved` | Sets `outcome` and `outcome_at`. `revision` also offers to add a `revision` date. |
| `revision` | `yes`, `no`, `moved` | `yes` creates the next revision version and sets `outcome` back to null, because the paper is under review again. The client then asks for the new notification date. |
| `camera_ready` | `yes`, `moved` | `yes` creates the camera-ready version. |

The outcome can also be set directly with `PATCH /api/projects/{ref} {outcome}`,
for a paper whose dates were never entered.

## 7. Versions

A version is a fixed, named state of the project's files. Its files never
change after it is made. It is a git tag in the project's history (§9) plus
a row with `id, project_id, number, kind, label, tag, commit, date_id, note, pdf_sha, pdf_name, created_at, created_by`.

| `kind` | Default label | Default tag |
| --- | --- | --- |
| `submission` | Submission | `submission` |
| `rebuttal` | Rebuttal | `rebuttal` |
| `revision` | Revision N | `revision-N` |
| `camera_ready` | Camera-ready | `camera-ready` |
| `arxiv` | arXiv | `arxiv` |
| `snapshot` | Snapshot N | `snapshot-N` |
| `other` | (label required) | the label as a slug |

- `number` counts all versions of the project from 1. The N in `Revision N`
  counts only revisions.
- A tag that exists already gets `-2`, `-3` appended (a second submission
  after a resubmission is `submission-2`).
- `at` says which state is frozen. It is `now` (the default) or `deadline`.
  `deadline` takes the last snapshot at or before the linked date's
  `due_at`, so answering "did you hand it in?" three days late still
  freezes what was there at the deadline. It can also be an ISO time or a
  commit. Creating a version with `now` first takes a fresh snapshot of the
  live document.
- `label` and `note` can be edited later. `kind`, `tag` and `commit` cannot.
- The PDF that was actually handed in can be attached
  (`PUT .../versions/{id}/pdf`). It is stored as a blob (§8). In phase 2b the
  server also compiles one for every version.
- Only the owner can delete a version. The tag goes, the commit stays in the
  history.

Every place that takes a ref (§11, Files) takes a version's tag, `HEAD` (the
last snapshot), `live` (the current document) or a commit.

## 8. The live document

Each project is one CRDT document (Yjs format, pycrdt on the server and in
the CLI, yjs in the browser). Every copy of it (the server, each
sync client, each browser tab) holds the whole document and exchanges small
updates. Concurrent edits merge on their own.

### Structure

```text
Y.Doc
└── "files": Y.Map<file id, Y.Map>
        "path": string        POSIX path relative to the project root
        "kind": "text" | "blob"
        "text": Y.Text        kind "text" only
        "sha":  string        kind "blob" only, SHA-256 hex of the content
        "size": int           kind "blob" only, bytes
```

- A file id is a UUID4 hex string (32 characters) chosen by whoever creates
  the file. A rename sets `path` and keeps the id, so edits made during a
  rename are not lost.
- Deleting a file removes its entry from `files`.
- A path is relative, uses `/`, has no empty, `.` or `..` segment, does not
  start with `.meertex/` and is at most 1024 bytes.
- Two entries can end up with the same path when two people create the same
  file offline. The entry with the lowest id keeps the path. Every other one
  is written out as `<stem>.conflict-<first 8 chars of id><suffix>`.
  Everybody computes the same answer, so nobody needs to repair the document.
- A file is text when it is valid UTF-8, contains no NUL byte and is at most
  `MAX_TEXT_BYTES` (2 MB). Everything else is a blob. Blob contents travel
  over HTTP (§11, Blobs) and must be uploaded before the entry that points
  at them is written.
- **Text offsets.** pycrdt counts text positions in UTF-8 bytes, Yjs in
  UTF-16 code units. Python code converts every character index before it
  calls `insert` or deletes (`len(s[:i].encode())`). Splitting a multibyte
  character corrupts the edit.

### On the server

- `app/live/` holds one room per open project. A room loads the document
  from the database on first use and unloads after `ROOM_IDLE_SECONDS`
  without a connection.
- Updates are stored in `doc_updates (id bigserial, project_id, data bytea, created_at)`.
  Incoming updates are buffered and written every 0.5 s as one merged row,
  and right away when a connection closes. Above 200 rows for one project
  they are compacted into a single row in one transaction.
- Every API write to files (§11, Files) is a CRDT edit made by the server,
  so connected clients get it like any other edit.

## 9. History (git)

The server keeps a git repository per project at
`DATA_DIR/projects/<id>/repo`, a normal repository with a working tree that
only meertex writes.

- A **snapshot** writes the live document into the working tree and commits
  it, if anything changed. It runs `SNAPSHOT_IDLE_SECONDS` (60) after the
  last edit, or `SNAPSHOT_MAX_SECONDS` (600) after the first edit not yet in
  a commit, whichever comes first. It also runs on demand for a version and
  when a room unloads.
- The commit message names who edited since the last snapshot
  (`Edits by didi@example.com, API`). The author is `meertex <meertex@localhost>`.
- Versions are git tags (§7).
- Git runs as a subprocess with an argument list, never through a shell,
  with `--` before paths.
- `GET /api/projects/{ref}/history` lists the commits.

## 10. Sync client and protocol

### Protocol

`GET /api/projects/{ref}/sync` upgrades to a WebSocket. It speaks the
y-websocket protocol, so the browser's `y-websocket` provider (the editor) and
pycrdt's `Provider` both work unchanged.

- Authentication is the session cookie, `Authorization: Bearer <token>` or
  `?token=`.
- Messages are `[0, sync step 1 | sync step 2 | update]` and `[1, awareness]`.
- The server sends sync step 1 on connect and answers the client's step 1
  with step 2. Every update it accepts is applied, stored (§8) and sent to
  the project's other connections.
- `reviewer` and `viewer` connections are read-only. Their step 2 and update
  messages are dropped.
- Close codes are `4001` (not signed in), `4004` (no such project, or not a
  member, also sent to open connections of someone who was just removed),
  `4009` (your role changed, connect again), `4400` (a message that is not
  valid) and `1013` (the client stopped reading and 128 MB piled up for it,
  connect again). Clients stop on 4001, 4004 and 4400 and reconnect on
  anything else.
- Awareness states (cursors and names) are passed on with their `user`
  field's `name` and `email` replaced by the signed-in person's, so nobody
  can show up as someone else.
- A message is at most `MAX_SYNC_MESSAGE_BYTES` (64 MB). The first sync of a
  checkout carries the whole document in one message.

### The browser editor

`/p/<name>/edit[/<path>]` edits the live document in the browser
(`app/static/js/pages/editor.js`). CodeMirror 6 is bound to an entry's
`Y.Text` with y-codemirror.next, so every keystroke is a CRDT edit like the
sync client's, and other people's cursors show with their names (awareness).

- The libraries come as one vendored ES module,
  `app/static/vendor/editor/editor.js`, built by `make editor` from
  `tools/editor/` (exact versions, one copy of yjs). The app itself keeps no
  build step. The server compresses responses, which takes the bundle from
  430 KB to 140 KB.
- The page uses y-websocket's `WebsocketProvider` with
  `resyncInterval: 15000`. y-websocket drops a connection that received
  nothing for 30 s, and a person alone on a paper receives nothing else.
- The file tree follows `materialize` (ported to `app/static/js/livefiles.js`,
  checked against the Python on generated documents). New files, renames,
  deletes and uploads go through the file routes (§11), which check the path
  and come back over the socket as CRDT edits. New files use
  `If-None-Match: *`.
- Viewers and reviewers get a read-only editor. A role change closes the
  socket with 4009 and the page says to reload.

### The CLI's sync client

`meertex sync` keeps every checkout registered on this machine in step with
the server.

A checkout is a folder with this layout:

```text
~/papers/2027-icse-green-llm/
├── .meertex/
│   ├── project.json    {project_id, name, server, main_file}
│   ├── state.bin       the document as of the last sync (full Yjs update)
│   ├── index.json      per path: file id, content hash, mtime/size/inode
│   ├── status.json     written by the client: pid, mode, connected,
│   │                   last_sync_at, unsent, pending, last_error, held
│   ├── lock            flock, so only one client syncs a folder
│   ├── request         `sync --once` asks a running service to reconcile
│   ├── hold            the checkout is held, since this UTC time (Holding)
│   ├── tmp/            atomic writes
│   └── trash/          files removed by someone else, by time of removal
├── .meertexignore      optional, gitignore syntax, synced like any file
└── main.tex ...
```

`~/.meertex/checkouts.json` lists every checkout on the machine.
`~/.meertex/token` holds the API token and `~/.meertex/config.toml` the
server address. `MEERTEX_SERVER` and `MEERTEX_TOKEN` override both.

**Ignored files** are never sent. These are always ignored, on top of
`.meertexignore`:

```text
.meertex/  .git/  .claude/  .venv/  venv/  node_modules/  __pycache__/
.DS_Store  *~  *.swp  .#*
*.aux *.bbl *.bcf *.blg *.dvi *.fdb_latexmk *.fls *.lof *.log *.lot *.nav
*.out *.run.xml *.snm *.synctex.gz *.toc *.vrb *.xdv
/<main_file stem>.pdf      (only at the root, so a figure sub/main.pdf syncs)
```

`index.json` says which shadows `state.bin` really holds, so a restart with a
blob download still pending is not mistaken for a local edit. Its stat cache
lets a scan skip unchanged files, which matters in folders with data.

Files over `MAX_BLOB_BYTES` (50 MB) are skipped with a warning. Remote files
are always written, even when a local rule would ignore them.

**The algorithm.** For each file the client keeps a *shadow*, the text as it
was last read from or written to disk. All work for one checkout runs in a
single task, so nothing interleaves between the steps below.

- *Local change.* Read the file. If it equals the shadow, stop. If the
  document's text still equals the shadow, apply the character diff
  shadow → disk to the document. Otherwise someone else's edit arrived in
  between. Make a patch shadow → disk (diff-match-patch), apply it to the
  document's text, apply the diff document → merged result to the document,
  and write the merged result to disk. The shadow becomes what is on disk.
- *Remote change.* First handle any local change to the same file as above.
  Then write the document's text to disk atomically (temporary file in
  `.meertex/`, then `os.replace`) and update the shadow.
- *New local file* creates an entry with a new id. *Deleted local file*
  removes the entry. A delete and a create with identical contents in one
  batch is a rename and keeps the id.
- *Remote delete* moves the local file to `.meertex/trash/<UTC time>/<path>`.
  If the local file has changes the document does not have, the local file
  wins and is sent again as a new entry.
- *Blobs* compare SHA-256. A local change uploads the blob, then sets
  `sha` and `size`. A remote change downloads it. The last writer wins.
- After every batch the document goes to `state.bin`. On start the client
  first compares the disk with `state.bin`, which catches what changed while
  it was not running, and then connects.

`meertex sync --once` syncs every checkout (or the given ones), waits until
nothing is pending, and exits. Agents use it before and after a batch of
work. When the sync service already holds a checkout, `--once` asks it to
reconcile instead (`.meertex/request`). It exits 0 when in sync, 1 when not
(no connection, blob transfers pending) and 2 on a usage error.

#### Holding

A checkout can be **held** for a large rewrite that coauthors should not see
half done. While it is held, the local changes stay local and other people's
edits keep arriving. Release sends everything that was held in one go.

- The state is the marker `.meertex/hold`, which holds the UTC time it was
  set, so a running service and the CLI agree on it. `meertex hold [DIR...]`
  writes it and `meertex release [DIR...]` removes it, by default for the
  checkout that contains the current folder. `meertex sync --hold [DIR...]`
  holds the given or current checkouts and then syncs them.
- A running sync client follows the marker within a second. The engine checks
  it in every step, and the watcher reports it.
- While held, the client writes nothing local into the document: no text
  diffs, no new files, no deletes, no renames, no blob uploads. Whatever is in
  the document would also go out in the next sync handshake.
- Other people's changes still arrive, around the local ones, which stay the
  difference between shadow and disk:
  - a file nobody touched here is written as usual, and a remote delete of it
    goes to the trash
  - a file changed here and by nobody else stays as it is
  - a file changed here and by someone else gets
    `merge_text(shadow, disk, document)` on disk, and the shadow becomes the
    document's text, so the local change is still exactly the difference
  - a held blob change stays on disk
  - a remote delete of a file with held changes keeps the local file, which
    goes out as a new file on release
  - a remote rename moves the file on disk and keeps the held change
  - a file that is new here and arrives from someone else under the same path
    keeps the local one, and release makes the usual conflict copy.
- `release` removes the marker and asks a running service to reconcile
  (`.meertex/request`), or reconciles like `sync --once` when none runs. The
  ordinary step then sends every held change.
- `sync --once` on a held checkout pulls but does not push, and says so.
- `status.json` carries `held`, `held_since` and `held_files` (how many files
  have held changes). `meertex status` shows the same for every checkout.
- A restart keeps holding. The marker, the shadows and the disk all survive it.

## 11. HTTP API

Every route under `/api` takes the session cookie or the API token, except
sign-in and the shared timeline (§3, Sharing the timeline). `{ref}` is a
project's id or its name. A name is looked up among
the projects you can see. A name that matches more than one is 409, with the
candidates in `detail`. Errors are `{detail}`. A refused write is 422 with
the reason.

### Object shapes

Every route returns these shapes. Datetimes are ISO 8601 UTC with `Z`.

```text
user      {id, email, name, timezone, api_token, naming_pattern, writing_days,
           reminders, reminder_days, reminder_hour, created_at}
person    {id, email, name}
folder    {id, name, parent_id, path, position}
date      {id, kind, label, due_at, tz, date_only, local, passed, seconds_left,
           answer, answered_at, changes: [{at, by, from, to}]}
next_date {id, kind, label, due_at, tz, date_only, local, seconds_left}
question  {project_id, project_name, date_id, kind, label, due_at, text, answers: [string]}
version   {id, number, kind, label, tag, commit, date_id, note, pdf_name,
           created_at, created_by: person | null,
           compiled: {id, status, pdf_url, log_url} | null}
member    {user_id, email, name, role, added_at}
file      {id, path, kind, size, sha}
commit    {commit, parent, at, message, editors: [string], files_changed}
diff      {from, to, files: [{path, old_path, status, binary, diff}]}
```

- `date.passed` is `due_at < now`. `seconds_left` is negative once passed.
- `question.text` is the full sentence, e.g. "The paper submission deadline
  passed on 15 Jan 2027. Did you hand it in?". `answers` are the values
  `POST .../answer` takes for it.
- `version.pdf_name` is null when no PDF is attached.
- `file.id` is null for refs other than `live`. `file.sha` is the SHA-256
  of the content for blobs and the git blob id for text at a commit.
  `file.size` is in bytes.
- `diff` text is unified diff with 3 lines of context, or git's
  `--word-diff=porcelain` output when `word=true`.

### Auth (meerpad's)

- `POST /api/auth/login {email, client?, next?}`
- `POST /api/auth/verify-code {email, code}`
- `GET /api/auth/callback?token=`
- `GET/PATCH /api/auth/me`. `PATCH` takes `name`, `timezone`, `naming_pattern`, `writing_days`,
  `reminders`, `reminder_days`, `reminder_hour` (§5, Reminders).
- `POST /api/auth/api-token`, `POST /api/auth/api-token/rotate`
- `POST /api/auth/logout`

### Folders

- `GET /api/folders` returns `[{id, name, parent_id, path, position}]`,
  depth first, siblings by position.
- `POST /api/folders {name, parent_id? | parent?}`. `parent` is a path.
- `PATCH /api/folders/{id} {name?, parent_id?, position?}`
- `DELETE /api/folders/{id}`. 409 while it holds anything.

### Projects

- A **summary** is
  `{id, name, title, paper, conference, year, track, outcome, labels, archived, planned, role, folder_id, folder_path, next_date, question_count, open_threads, writing_days, own_writing_days, writing, reminders_muted, updated_at, url}`.
  `question_count` is how many questions are waiting. `reminders_muted` says
  *you* muted the reminder mails for it (§5, Reminders). `folder_id` and
  `folder_path` are where *you* put it (null and `""` at the top level). `url` is
  `<BASE_URL>/p/<name>`.
- `GET /api/projects` returns summaries. Filters are all optional.
  - `outcome` (comma separated, `none` for null). `GET /api/projects?outcome=accepted`
    is "all my accepted papers".
  - `label`, `folder` (id or path, including subfolders), `q` (name, title,
    conference, notes), `year`, `conference`.
  - `archived` is `false` (default), `true` or `all`.
  - Order is the folder tree's when `folder` is given, else next date first,
    then the most recently updated.
- `POST /api/projects` with the fields of §4 plus `dates: [...]` (§5),
  `folder` (id or path), `labels`, `name` (overrides the pattern),
  `starter` (`empty`, the default, or `basic` for a `main.tex` and a
  `refs.bib`) and `dry_run`. Returns 201 and the full project. A folder
  path that does not exist yet is created. `dry_run` returns 200 and
  `{name, dry_run: true}`.
- `GET /api/projects/{ref}` returns the summary plus every field of §4,
  `owner` (person), `dates`, `questions` (the full list), `versions`,
  `members`, `forked_from` (`{id, name}` or null) and `file_count`.
- `PATCH /api/projects/{ref}` takes any field of §4 except the bookkeeping
  ones. Editors and the owner only.
- `DELETE /api/projects/{ref}?confirm=<name>` deletes the project with its
  history and files. Owner only. A wrong `confirm` is 422.
- `POST /api/projects/{ref}/fork` takes the fields of `POST /api/projects`.
- `PUT /api/projects/{ref}/placement {folder_id, position?}` moves it in
  *your* tree.
- `PUT /api/projects/{ref}/reminders {muted}` mutes or unmutes *your*
  reminder mails for it (§5, Reminders). Any member may. Returns the summary.
- `GET /api/reminders/preview` returns what your next reminder mail would say,
  as `{reminders, reminder_days, reminder_hour, timezone, sending, last_at, next_at,
  projects: [{id, name, title, conference, year, planned, role, url, items}],
  muted: [{id, name}], subject, body, timeline_url, settings_url}`. An item is
  `{type, date_id, kind, label, due_at, tz, date_only, local, when, text}` with
  `days_left` and `threshold` for `type` `date`, `start`, `writing_days` and
  `days_left` for `writing`, and `answers` for `question`. `sending` is false
  while `REMINDERS_ENABLED` is off. `subject` and `body` are null when there is
  nothing to say.
- `GET /api/questions` lists the waiting questions of the projects where you
  are owner or editor, archived ones left out.

### Dates

- `POST /api/projects/{ref}/dates {kind, label?, due_at | date, tz?}`
- `PATCH /api/projects/{ref}/dates/{id}` with the same fields.
- `DELETE /api/projects/{ref}/dates/{id}`
- `POST /api/projects/{ref}/dates/{id}/answer {answer, due_at?, date?, tz?, version?: {at?, note?}}`
  returns the project.

### Members

- `GET /api/projects/{ref}/members` returns `[{user_id, email, name, role, added_at}]`.
- `POST /api/projects/{ref}/members {email, role}`. Editors can add
  `reviewer` and `viewer`. Only the owner can add `editor`.
- `PATCH /api/projects/{ref}/members/{user_id} {role}` and `DELETE`. Owner
  only. The owner cannot be removed or changed.

### Versions

- `GET /api/projects/{ref}/versions`
- `POST /api/projects/{ref}/versions {kind, label?, note?, date_id?, at?}`
- `GET /api/projects/{ref}/versions/{id or tag}` adds `files`.
- `PATCH /api/projects/{ref}/versions/{id or tag} {label?, note?}`
- `DELETE /api/projects/{ref}/versions/{id or tag}`. Owner only.
- `PUT /api/projects/{ref}/versions/{id or tag}/pdf` with the PDF as the body
  (`?name=` for its file name). `GET` returns it.

### Files

- `GET /api/projects/{ref}/files?ref=live` lists `[{path, kind, size, sha, id}]`.
  `ref` is `live` (the default), `HEAD`, a tag or a commit.
- `GET /api/projects/{ref}/files/{path}?ref=live` returns the content.
- `PUT /api/projects/{ref}/files/{path}` writes the body to the live
  document. Text is applied as a diff, so concurrent edits survive.
  Editors and the owner only. With `If-None-Match: *` it only creates: a
  file that is there already stays as it is and the answer is 412.
- `DELETE /api/projects/{ref}/files/{path}`
- `POST /api/projects/{ref}/files/move {from, to}`
- `GET /api/projects/{ref}/zip?ref=live` downloads the files as a zip,
  named `<name>-<ref>.zip`.
- `GET /api/projects/{ref}/diff?from=<ref>&to=<ref>&word=false` returns
  `{files: [{path, status, diff}]}` with `status` one of `added`, `deleted`,
  `modified`, `renamed`. `word=true` gives a word diff, which is what LaTeX
  paragraphs need.
- `GET /api/projects/{ref}/history?limit=50` returns
  `[{commit, parent, at, message, editors, files_changed}]`, newest first.
  `parent` is the first parent, null for the first commit.

### Blobs

- `PUT /api/projects/{ref}/blobs/{sha}` stores the body. The server checks
  the hash and `MAX_BLOB_BYTES`. Editors and the owner only.
- `GET /api/projects/{ref}/blobs/{sha}`

### Sync

- `GET /api/projects/{ref}/sync` (WebSocket, §10).

## 12. CLI

`meertex` is a Python package in `cli/`, published to PyPI as `meertex`
on every push to `main` that changes it (README.md, Releasing the CLI). It
is installed with `pipx install meertex` (or `uv tool install meertex`), and
from a clone with `pipx install ./cli`. Every command takes `--json`.
Projects are addressed by name or id.

| Command | What it does |
| --- | --- |
| `meertex login [--server URL] [--email E]` | Mails a code, asks for it, stores an API token in `~/.meertex/token`. |
| `meertex new --planned --conference C --year Y [--date ...]` / `meertex start <project> --title T [--paper P] [--starter basic]` | A plan for a venue, and turning it into a paper (§4, Planned papers). |
| `meertex timeline [--writing-days N]` | The timeline as text (§3, The timeline). `--writing-days` sets your usual writing time first. |
| `meertex reminders` | What your next reminder mail would say (§5, Reminders): dates coming up, when to start writing, waiting questions. |
| `meertex projects [--outcome O] [--label L] [--folder F] [--q TEXT] [--year Y] [--conference C] [--archived \| --all]` | The project list with countdowns. `--archived` shows only archived projects, `--all` both. |
| `meertex show <project>` | One project with its dates, questions and versions. |
| `meertex new --title T --conference C --year Y [--paper P] [--date kind=YYYY-MM-DD[THH:MM][@tz]]... [--set field=value]... [--folder F] [--from-json FILE] [--starter basic \| --zip FILE] [--checkout [DIR]] [--dry-run]` | Creates a project and, with `--checkout`, checks it out into `~/papers/<name>` (or DIR). `--from-json` takes the body of `POST /api/projects`. `--zip` imports a publisher's template zip (§4, Starting files). When the import fails, the project is deleted again. |
| `meertex fork <project> [same flags as new]` | Resubmission to another venue (`POST /api/projects/{ref}/fork`), with `--checkout` like `new`. |
| `meertex link <dir> [--project P]` | Turns an existing folder into a checkout. Without `--project` it creates one from the folder's contents (asks for the fields it needs). With `--project` the project must be empty. |
| `meertex checkout <project> [DIR]` | Writes the project into DIR (default `~/papers/<name>`), which must be empty or missing, and registers it. |
| `meertex sync [DIR...] [--once] [--hold]` | The sync client (§10). `--hold` holds the given or current checkouts first. |
| `meertex hold [DIR...]` / `meertex release [DIR...]` | Keeps a checkout's changes local while other people's edits still arrive, and sends them all in one go (§10, Holding). By default the checkout that contains the current folder. |
| `meertex status` | Every checkout with its sync state, held ones with how many files have held changes and since when. |
| `meertex set <project> field=value...` | Edits fields. |
| `meertex date <project> add\|set\|rm ...` | Edits dates. |
| `meertex questions` / `meertex answer <project> <date id or kind> <answer> [--date D] [--at deadline\|now]` | The waiting questions, and answering one. |
| `meertex versions <project>` / `meertex version <project> <kind> [--label L] [--note N] [--pdf FILE] [--at A]` | Lists versions, makes one. |
| `meertex diff <project> <from> [<to>] [--word]` | Diff between refs. `to` defaults to `live`. |
| `meertex folders` / `meertex folder add\|mv\|rm ...` / `meertex mv <project> <folder>` | The folder tree. |
| `meertex service install\|uninstall` | A systemd user unit that runs `meertex sync`. |
| `meertex refs <project> [check\|add\|mark\|unmark\|set ...]` | The reference check (§16, CLI). |

## 13. Reviews

GitHub's pull request review, for a paper. A reviewer starts a review on a
fixed state, comments on spans of text (or clicks in the PDF), and submits the
review with a verdict. The authors reply, change the text, and resolve each
thread. Resolving records how the span changed, so the reviewer sees the
request, the answer and the change side by side, and accepts or reopens.

### Objects

```text
review   {id, reviewer: person, state, verdict, summary, commit,
          version: {id, tag, label} | null, created_at, submitted_at,
          counts: {open, resolved, accepted}}
thread   {id, review_id, state, pending, created_by: person, created_at, updated_at,
          file_id, path, start, end, start16, end16, line_start, line_end,
          text, quote, changed, outdated,
          base: {commit, path, start, end, line_start, line_end},
          resolution: {by: person, at, commit, note, diff} | null,
          accepted: {by: person, at} | null,
          comments: [comment]}
comment  {id, author: person, kind, body, suggestion, created_at, edited_at}
```

- `review.state` is `pending` or `submitted`. `verdict` is null while
  pending, then `approve`, `comment` or `changes_requested`.
- `thread.state` is `open`, `resolved` or `accepted`. `pending` is true for a
  thread of a review that is not submitted yet. Only its reviewer sees it.
- `comment.kind` is `comment`, or one of the events `resolved`, `accepted`,
  `reopened`, `applied` that the timeline shows between comments. An event's
  `body` is the note given with it, possibly empty.
- `comment.suggestion` is replacement text for the thread's span, or null.
  An empty string is a suggestion too, to delete the span.
- `resolution` is null while the thread is open, and `accepted` is null
  unless it is accepted. Reopening clears both. A thread can be accepted
  without having been resolved, then `resolution` stays null.

### Anchoring

pycrdt's sticky indexes cannot be used (§8, ydoc.py). A thread is anchored
to a snapshot instead, and mapped to the live text by diffing.

- Every thread has a **base**: a commit, the file's path there, and the span
  `[start, end)` in that file's text, in Unicode code points (Python string
  indexes). `quote` is the text of that span, `line_start` and `line_end`
  (1-based, inclusive) its lines. A thread made on `live` (from the editor)
  takes a snapshot first, so its base is that commit.
- `file_id` is the live document's entry for the base path when the thread
  was made. It survives renames. A thread made on an older commit, on a file
  renamed since, finds the entry through git's rename detection from that
  commit to a snapshot of now. When `file_id` is null, or no longer in the
  live document, the live file at the base path is used.
- Whole lines (`line_start`, `line_end`) span from the start of the first line
  to the end of the last, without its newline (and without a `\r` before it).
  A final newline does not start another line, and an empty file has one
  empty line.
- The **current span** is found per request: the base text and the live text
  of `file_id` are diffed (diff-match-patch), and `start` and `end` are
  mapped with `diff_xIndex`. `path`, `start`, `end`, `line_start`,
  `line_end` and `text` describe the current span. `start16` and `end16` are
  the same span in UTF-16 units, which the editor uses. Base texts are cached
  per commit and path, and diffs per base and file, for one request
  (`app/anchors.py`).
- `diff_xIndex` moves a position inside deleted text to where the deletion
  was, in front of what replaced it. So a rewritten span maps onto the new
  words. Text that the diff inserts exactly at the span's end joins it, text
  inserted exactly at its start does not.
- `outdated` is true when the file is gone, or the span mapped to nothing
  while the quote was not empty. Then `path`, `start`, `end`, the lines and
  `start16`/`end16` are the base's (UTF-16 counted in the base text), and
  `text` is empty. `changed` is true when `text` differs from `quote`.
- A thread's base need not be its review's commit. One made on `live` while
  the review is on an older commit gets a newer base, so the review page
  places threads by `base` only when `base.commit` is the review's commit.

### Lifecycle and roles

| Action | Who |
| --- | --- |
| Start a review, comment, reply, reopen | reviewer, editor, owner |
| Resolve, apply a suggestion | editor, owner |
| Accept | the thread's creator, or the owner |
| Edit or delete a comment | its author |

- A user has at most one pending review per project. Starting a second one
  returns the pending one.
- A thread made with the `review_id` of your pending review is pending until
  you submit. Without `review_id` it is a single comment, visible at once.
  Any other `review_id` is 422.
- Only the reviewer submits or discards a review (403 for anybody else). A
  submitted review cannot be submitted again or discarded (422).
- A pending thread takes replies from its reviewer, but it cannot be
  resolved, applied, accepted or reopened before the review is submitted
  (422).
- Resolve and apply need an open thread. Accept needs an open or a resolved
  one. Reopen needs a resolved or an accepted one. Anything else is 422.
- **Resolve** takes a snapshot, and stores the word diff (git's
  `--word-diff=porcelain` format) between `quote` and the current `text`, the
  commit and an optional note. **Apply** writes a suggestion over the current
  span in the live document (a CRDT edit through the room) and resolves with
  the note "Applied the suggestion". Its diff is between `quote` and the
  suggestion. A thread whose text is gone (outdated) cannot take a suggestion
  (422). **Reopen** sets the state back to open. Each adds an event to the
  thread's comments (`resolved`, `applied`, `accepted`, `reopened`).
- The stored diff is only git's hunk, without the header. Its `@@` line
  counts from the base's and the current `line_start`, so it shows the
  file's line numbers.
- Events cannot be edited or deleted (422). A thread goes with its last
  comment of kind `comment`.
- **Notifications.** Submitting a review mails the owner and the editors,
  with a link to `<BASE_URL>/p/<name>/reviews/<review id>`. A new single
  comment mails them too. A reply, a resolve, an apply, an accept or a reopen
  mails everybody who wrote in the thread (comments and events). Mails only go
  to current members, never to the person who acted, and never about a
  pending review. Thread mails link to
  `<BASE_URL>/p/<name>/reviews/<review id>#thread-<thread id>`, or to
  `<BASE_URL>/p/<name>/reviews#thread-<thread id>` for a single comment.

### Routes

- `GET /api/projects/{ref}/reviews` lists the submitted reviews and your own
  pending one, newest first.
- `POST /api/projects/{ref}/reviews {ref?}` starts a review. `ref` is `HEAD`
  (the default), a version's tag, a commit or `live` (snapshot now). 201, or
  200 with your existing pending review. Its `version` is the one whose tag
  was given, else the newest version at that commit.
- `GET /api/projects/{ref}/reviews/{id}` returns the review and its threads.
- `POST /api/projects/{ref}/reviews/{id}/submit {verdict, summary?}`
- `DELETE /api/projects/{ref}/reviews/{id}` discards a pending review and its
  threads.
- `GET /api/projects/{ref}/reviews/{id}/changes?word=true` is the diff from
  the review's commit to `live` (§11 diff). The PDF of the same change is
  `POST /api/projects/{ref}/pdfdiff {from: <review commit>, to: "live"}` (§14).
- `GET /api/projects/{ref}/threads?state=open&review=&path=` lists threads in
  file order. `state` is `open` (default), `resolved`, `accepted` or `all`.
  `path` is the file's current path.
- `GET /api/threads?state=open` does the same across all your projects. Each
  thread then also has `project: {id, name, title}`.
- `POST /api/projects/{ref}/threads {path, ref?, start?, end?, line_start?, line_end?, body, suggestion?, review_id?}`
  makes a thread and returns it (201). `ref` is what the offsets refer to
  (default `live`). Give `start` and `end`, or `line_start` and `line_end`
  for whole lines (`line_end` defaults to `line_start`). A comment needs a
  `body` or a `suggestion`. A file that is not at `ref` is 404. A file that
  is not text, or a span outside it, is 422.
- `GET /api/projects/{ref}/threads/{id}`. Ids of threads, reviews and
  comments may be given as a prefix of at least 8 characters everywhere. A
  shorter one is 422, one that fits several is 409 with the `candidates`.
  Someone else's pending review and its threads are 404.
- `POST /api/projects/{ref}/threads/{id}/comments {body, suggestion?}`
  returns the thread (201).
- `PATCH /api/projects/{ref}/comments/{id} {body?, suggestion?}` returns the
  thread. `suggestion: null` removes it. `DELETE` returns `{ok, thread}`, and
  `thread` is null when the thread went with its only comment.
- `POST /api/projects/{ref}/threads/{id}/resolve {note?}`, `/accept {note?}`,
  `/reopen {note?}`, `/apply {comment_id?}` (the newest suggestion when
  omitted). Each returns the thread.

### CLI

| Command | |
| --- | --- |
| `meertex reviews <project>` | The reviews. |
| `meertex threads [<project>] [--state S] [--review ID] [--path P]` | Open threads (all projects without a project), each with its file and current lines, the quote, the current text, the conversation and any suggestion. |
| `meertex thread <project> <id>` | One thread. |
| `meertex reply <project> <id> TEXT [--suggestion TEXT]` | |
| `meertex resolve <project> <id> [--note TEXT]`, `accept`, `reopen [--note]` | |
| `meertex apply <project> <id> [--comment ID]` | Apply a suggestion. |
| `meertex comment <project> <path>:<line>[-<line>] TEXT [--suggestion TEXT] [--review]` | A comment on lines of the live text. `--review` adds it to your pending review, starting one on `HEAD` if needed. |
| `meertex review <project> start [--ref R]`, `submit --verdict V [--summary S]`, `discard` | |

`TEXT`, `--suggestion`, `--note` and `--summary` take `-` to read the text
from stdin, for one of them per call. `meertex comment` reads the live file
first, so lines that are not there fail before anything is written. The
commands are in `cli/meertex/cmd_reviews.py`.

### In the web app

- The **Reviews** tab lists reviews and open conversations, and starts a
  review (on the current state, `HEAD` or a version).
- A **review page** has three views. *Conversation*: every thread with its
  snippet, comments, suggestions, resolution diff and the buttons for the
  next step. *Files*: the source at the review's commit with line numbers, its
  threads under their lines, and the PDF of that commit next to it. Selecting
  text, or double-clicking the PDF (SyncTeX finds the line), opens a comment
  box with an optional suggestion. *Changes since this review*: the word diff
  to `live` and the latexdiff PDF. A pending review has a "Finish your review"
  dialog with the verdict and a summary.
- The **editor** shows open threads as highlighted spans in the text (from
  `start16`/`end16`, moved along with every edit and read again from the
  server a few seconds after edits settle) and in a Comments panel, where
  authors reply, resolve and apply suggestions. Selecting text shows a
  floating "Comment" button (also Ctrl+Alt+M), so selecting to copy does not
  open anything. In the review's *Files* view, clicking a line number (shift
  for a range) opens the comment box at once. That view also shows single
  comments and other reviews' threads made on the same commit.
- The project summary's `open_threads` counts the open threads you can see,
  for the Reviews tab's counter and the overview.

## 14. Compiling *(phase 2b)*

A container of its own compiles LaTeX. The server never runs TeX.

### The compile service (`compile/`)

- A TeX Live image (`texlive/texlive:TL2025-historic`, pinned by digest)
  with a small HTTP service on port 8000 (`compile/service.py`, standard
  library only), run as an unprivileged user. Only the `web` container
  reaches it, and every request but `GET /healthz` carries
  `Authorization: Bearer <COMPILE_TOKEN>`.
- Each job runs in a fresh folder with `latexmk -norc` (a `latexmkrc` is
  Perl), the project's compiler (`pdflatex`, `xelatex` or `lualatex`),
  `-interaction=nonstopmode`, `-file-line-error`, `-synctex=1`, without shell
  escape and with `openin_any=p` and `openout_any=p`, so a document cannot
  read or write outside its folder. TeX runs in the main file's folder.
  lualatex also gets `TEXMFOUTPUT` set to TeX Live's root, because LuaTeX 1.22
  holds Lua's `io.open` to the paranoid rules and luaotfload reads TeX Live's
  own files by absolute path. A job that runs longer than
  `COMPILE_TIMEOUT_SECONDS` (90) is killed with its process group. The newest
  `COMPILE_KEEP_JOBS` (30) folders are kept for SyncTeX.
- `POST /jobs?main_file=&compiler=` takes a tar.gz of the files as the body
  and returns `{job_id, status, duration_ms, log}`. `status` is `success` (a
  PDF without errors), `errors` (a PDF, but TeX or latexmk reported errors),
  `failure` (no PDF) or `timeout`. An upload with absolute paths, `..`, links
  or device files, or above the size caps, is refused with 400.
- `GET /jobs/{id}/output.pdf`, `GET /jobs/{id}/output.log`. 404 for a job the
  service no longer has.
- `POST /jobs/{id}/synctex/edit {page, x, y}` returns `{file, line, column}`,
  or 422 when SyncTeX knows nothing there.
  `POST /jobs/{id}/synctex/view {file, line, column}` returns
  `[{page, x, y, width, height}]`. Coordinates are PDF points from the
  page's top left.
- `POST /latexdiff?main_file=&compiler=` takes one tar.gz with the two trees
  as `old/` and `new/`, runs `latexdiff --flatten` on the two main files and
  compiles the result like `/jobs`. latexdiff is Perl and the paranoid rules
  do not bind it, so a tree that `\input`s an absolute path or a path with
  `..` is refused first (`failure`).
- biber is Perl as well. latexmk runs it through a guard that reads the
  `.bcf` and refuses a bibliography that is absolute, has `..` or is a URL,
  and biber runs with `--noconf`. The main file's name is limited to letters,
  digits, spaces and `. , + = @ -`, because latexmk hands it to a shell.

### On the server

- `COMPILE_URL` (empty turns compiling off), `COMPILE_TOKEN`,
  `COMPILE_TIMEOUT_SECONDS`. The routes answer 503 with the reason when
  compiling is off, or the service cannot be reached, refuses the token or is
  busy.
- A compile takes the files at a ref (`live` from the room and the blob store,
  anything else from git) and hashes them with `main_file` and `compiler`. A
  finished compile (`success`, `errors` or `failure`) with the same hash is
  reused. For another ref its result is copied into a row of its own, so
  `ref` and `commit` stay right. One compile per project runs at a time, and
  a request for a hash that is being compiled waits for it. The request that
  compiles waits for TeX and gets the finished compile.
- Rows `compiles (id, project_id, kind, ref, commit, content_hash, compiler, main_file, status, duration_ms, log, problems, pdf_sha, job_id, created_at, created_by)`.
  `kind` is `pdf` or `pdfdiff`. `ref` is the ref as asked for (`from..to` for
  a pdfdiff). `commit` is null for `live`. The PDF is a project blob. `log`
  keeps the last 512 KB. `status` is also `running`, or `unavailable` for a
  version's compile that found no service (or was cut off by a restart).
- `problems` come from the log: `[{level, path, line, message}]`, `level`
  being `error`, `warning` or `badbox`, paths relative to the project. `path`
  and `line` are null for TeX Live's own files and for messages without a
  place.
- New project field `compiler`: `pdflatex` (default), `xelatex`, `lualatex`.

```text
compile {id, kind, status, ref, commit, compiler, main_file, duration_ms,
         created_at, problems, pdf_url, log_url}
```

`pdf_url` (null without a PDF) and `log_url` are paths on this server.

- `POST /api/projects/{ref}/compile {ref?}` (default `live`). Viewers too,
  because reviewers read the PDF.
- `GET /api/projects/{ref}/compiles/latest?ref=live`, the newest compile of
  kind `pdf`, or `null` (200) when there is none yet, because the editor asks
  every time it opens a paper. A tag or a commit finds every compile of that
  commit. A ref that names nothing is 404.
- `GET /api/projects/{ref}/compiles/{id}`, `/pdf` (inline), `/log` (text).
- `POST /api/projects/{ref}/compiles/{id}/synctex/edit {page, x, y}` returns
  `{path, line, column}`, 404 when SyncTeX knows nothing there.
  `/synctex/view {path, line, column}` returns
  `{boxes: [{page, x, y, width, height}]}`. 410 when the compile service no
  longer has the job.
- `POST /api/projects/{ref}/pdfdiff {from, to}` returns a compile of kind
  `pdfdiff`, cached like any other.
- Making a version compiles its commit in the background. The version gets
  a `running` compile in its own transaction, and a thread starts it once the
  version is committed. The version then has
  `compiled: {id, status, pdf_url, log_url} | null` (null while compiling is
  off).

### CLI

`meertex compile <project> [--ref R] [--out FILE]` compiles on the server,
prints the status and the problems as `path:line`, and saves the PDF. A
checkout's PDF goes to `<main file stem>.pdf` at its root, which sync ignores.
It exits 0 on `success` and 1 otherwise.

### In the editor

- A PDF pane next to the text (PDF.js, vendored like the editor bundle),
  with zoom, page count and download. It keeps its scroll position when the
  PDF is replaced.
- Compiling on Ctrl/Cmd-Enter, on the Recompile button, and on its own two
  seconds after the last edit (a toggle, on by default, remembered per
  browser).
- The problems of the last compile as a list. Clicking one opens the file at
  its line.
- Double-click in the PDF jumps to the source line (SyncTeX). A button jumps
  from the cursor to the PDF and marks the spot.
- LaTeX autocomplete: commands, environments after `\begin{`, `\ref` and
  `\eqref` labels from every `\label{}` in the project, `\cite` keys from
  every `.bib` file, and file paths in `\input`, `\include` and
  `\includegraphics`.
- Compile errors and warnings with a line are marked on their lines (a
  gutter icon, a red or amber line, the message on hover). Bad boxes stay in
  the problems list. The marks move with edits until the next compile.
- A double-click in the PDF selects the clicked word in the source, found in
  the SyncTeX line and the lines around it, and lights up the line and the
  word for a few seconds.

## 15. Deployment

Coolify deploys `docker-compose.coolify.yml` (meerpad's layout). It holds
`db` (postgres:18), `web` (the server, with git installed) and `compile`
(TeX Live, §14), which runs as an unprivileged user with a read-only root,
no capabilities and memory, CPU and process limits. Locally `compile` sits on
an internal network without internet access. On Coolify it shares the
project network, because custom networks fight Coolify's routing. Nothing a
job runs opens a connection: no shell escape, and biber only through a guard
that refuses URLs and paths outside the job.

| Variable | |
| --- | --- |
| `BASE_URL` | Required. The app's public address. |
| `SECRET_KEY` | Coolify magic variable. Signs login codes. |
| `DATABASE_URL` | Built from the Postgres magic password. |
| `DATA_DIR` | `/data`, a named volume. Back it up together with the database. |
| `SMTP_*`, `EMAIL_FROM` | As meerpad. Without `SMTP_HOST` mails go to the log. |
| `MAX_TEXT_BYTES`, `MAX_BLOB_BYTES`, `MAX_SYNC_MESSAGE_BYTES` | 2 MB, 50 MB, 64 MB. |
| `SNAPSHOT_IDLE_SECONDS`, `SNAPSHOT_MAX_SECONDS`, `ROOM_IDLE_SECONDS` | 60, 600, 300. |
| `REMINDERS_ENABLED` | `true`. `false` turns the reminder mails off (§5, Reminders). They need `SMTP_*` to reach anyone. |
| `REFERENCES_NETWORK` | `true`. `false` turns the reference lookups off (§16). |
| `REFERENCES_MAILTO` | The address the lookups give the services, `EMAIL_FROM` when empty. |
| `SEMANTIC_SCHOLAR_API_KEY` | Optional. A key of its own gets a larger rate limit. |
| `ANTHROPIC_API_KEY`, `REFERENCES_CLAUDE_MODEL` | Turn the claim check on (§16, Claims). Empty and `claude-opus-5-5`. |

Traefik passes WebSockets through without extra settings.

## 16. References

A paper's `.bib` files are checked against the publishers' records and the
web, so wrong and made-up citations show up before a reviewer finds them.
The `.bib` files stay the source. meertex writes into them only when someone
asks, to add an entry, to take a value from a record or to mark an entry as
checked. The code is in `app/references/`.

### Entries

- Every text file in the live document whose path ends in `.bib` (any case)
  is read. The parser (`bibtex.py`) knows `@string` macros (and the month
  macros `jan` to `dec`), `#` concatenation, braces and quotes as
  delimiters, and skips `@comment`, `@preamble` and text between entries.
  Field names are compared without case. Fields a `crossref` parent has and
  the entry does not are inherited, as bibtex does.
- An entry that cannot be parsed goes into the report's `broken` list with
  its file, its line and the reason, and parsing goes on after it.
- An entry's **fingerprint** is the SHA-256 of its type and its fields,
  without its key and without `meertex-checked`. Field names are lowercased
  and sorted. Values lose their outer delimiters, and every run of white
  space becomes one space. Reformatting, reordering fields or renaming the
  key keep the fingerprint. Changing a value does not.
- `\cite` keys come from every `.tex` file. The commands are `\cite`,
  `\nocite` and the natbib and biblatex variants (`\citep`, `\citet`,
  `\citealp`, `\citeauthor`, `\citeyear`, `\parencite`, `\textcite`,
  `\autocite`, `\footcite`, `\smartcite`, `\supercite`), also starred and
  capitalised, with their optional arguments, outside comments.
  `\nocite{*}` cites every entry.

### The checked mark

A person who checked an entry by hand marks it in the entry itself.

```bibtex
  meertex-checked = {2026-10-27, Didi Hoffmann, 3f2a9c1b},
```

- The value is the day (UTC), the name of the person who marked it (the
  account's name, else the part of the email before the `@`, without
  commas) and the first 8 hex digits of the entry's fingerprint.
- biber and bibtex ignore the field, so it travels with the file into
  checkouts, zips and Overleaf.
- While the hash is the entry's fingerprint, the entry is `checked`. When
  it is not, someone changed the entry after it was checked, and it is
  `changed`. A mark that cannot be read counts as one that does not match.
- Marking writes the field as the entry's last field and replaces an older
  mark. Unmarking removes it. Both are CRDT edits made by that person.

### Findings

A finding is `{code, level, field, message, value, record_value, source, url}`.

- `level` is `error`, `warning` or `info`. Info never changes a status.
- `field` is the field it is about, or null. `value` is the entry's value
  and `record_value` the record's, which "Take this value" writes.
- `source` names where it comes from: `bibtex` (the file itself), `search`
  (the title search over the indexes), `doi.org`, `crossref`, `semantic
  scholar`, `dblp`, `arxiv`, `openlibrary`, `web`, `wayback` or `claude`. `url` links to the record or to an archived copy, or is null.

These need no network. They are computed again on every read from the live
files, so they are never stale.

| Code | Level | When |
| --- | --- | --- |
| `duplicate_key` | error | Another entry in the project has the same key. |
| `duplicate` | warning | Another entry has the same DOI or almost the same title. |
| `placeholder` | error | A field is or holds a placeholder: `XXXX`, `??`, `TODO`, `TBD`, `FIXME`, `Author Name`, `Lastname`, `[citation needed]`, `et al.` in `author`, or only `...`. |
| `year` | error or warning | `year` (or the year of `date`) is not a year from 1450 on (warning), is later than this year (error), or is later than the project's `year` (warning). |
| `pages` | warning | The last page comes before the first. |
| `arxiv_id` | error | An arXiv id that cannot exist, because of its form or because its month is in the future. |
| `arxiv_year` | warning | A preprint entry whose year is not the year in its arXiv id. |
| `isbn` | error | The ISBN's check digit is wrong. |
| `doi_syntax` | error | The DOI does not look like `10.<digits>/<suffix>`. |
| `not_cited` | info | No `\cite` uses the key. |
| `urldate` | info or warning | An `online` entry has no `urldate` (info), or its `urldate` is in the future (warning). |

A **check run** looks every entry up, in these steps, and keeps the
findings per fingerprint.

1. **Identifiers.** The DOI comes from `doi`, else from a `doi.org` link in
   `url`. An arXiv id from `eprint` (with `eprinttype` or `archiveprefix`
   arXiv), from an `arxiv.org/abs/` or `/pdf/` link, or from `arXiv:<id>`
   in `journal`, `howpublished` or `note`, becomes the DOI
   `10.48550/arXiv.<id>` when there is no other DOI. An ISBN comes from
   `isbn`.
2. **The record.** A DOI is resolved at doi.org as CSL JSON, which covers
   Crossref and DataCite (arXiv, Zenodo). A DOI that does not resolve is
   `doi_not_found` (error). Its message names the owner of the DOI's prefix
   (Crossref's `/prefixes`) and says so when the entry names another
   publisher. A Crossref DOI is also read from Crossref's works API, for
   retractions, corrections and the published version of a preprint. An
   ISBN is looked up at Open Library.
3. **No identifier.** The title is searched in Crossref (with the first
   author and the year), Semantic Scholar, DBLP and arXiv, and a book also
   in Open Library. A hit is a record with nearly the same title that shares
   a family name with the entry's authors (or has no authors). A hit with a
   DOI adds `suggest_doi` (info). No hit while at least two sources answered
   is `not_found`, an error for `article`, `inproceedings`, `incollection`
   and `conference` and a warning for every other type. Its message says
   whether the first author has a paper with a similar title on Semantic
   Scholar, which catches real authors with an invented title. When fewer
   than two sources answered, the entry gets `unavailable` (info).
4. **The comparison** with the record. LaTeX is turned into text, and case,
   accents and punctuation do not count.

| Code | Level | When |
| --- | --- | --- |
| `doi_not_found` | error | doi.org does not know the DOI. |
| `doi_mismatch` | error | The DOI's record is another work. The title is far off and no author matches. |
| `title` | warning | The title differs from the record's (a missing subtitle, other words). |
| `authors` | error or warning | No author matches the record (error). Authors are missing, extra or in another order (warning). `and others` matches any rest. |
| `year` | warning | The year is none of the record's years (issued, in print, online). |
| `venue` | warning | `journal` or `booktitle` has no word in common with the record's venue, not even its acronym. |
| `differs` | warning | `volume`, `number` or `pages` differ from the record. |
| `missing` | info | The record has a `doi`, `pages`, `volume`, `number` or `publisher` that the entry lacks. |
| `suggest_doi` | info | The entry has no DOI, and the record that matched has one. |
| `retracted` | error | The work was retracted or withdrawn (Crossref, with Retraction Watch's data). |
| `corrected` | info | A correction or an erratum was published. |
| `published` | info | The entry cites a preprint that has been published since. `record_value` is the published version's DOI. |
| `not_found` | error or warning | No source has the work (step 3). |
| `unavailable` | info | Too few sources answered to tell. |

Last come the **links**. `url` (or a `\url{}` in `howpublished`) is fetched
when the entry has no DOI, or when it is an `online`, `misc`, `software`,
`electronic` or `www` entry.

| Code | Level | When |
| --- | --- | --- |
| `url_dead` | error | 404 or 410, or the host does not exist, refuses the connection or has a broken certificate. A GitHub link that is 404 is a repository that does not exist or is private. |
| `url_moved` | warning | The page says it is not found while answering 200, or a link with a path ends on the site's home page. |
| `url_blocked` | info | 401, 403, 429, a server error or a timeout. Bot protection answers like that, so it says nothing about the page. |
| `url_title` | info | The page's title has no word in common with the entry's title. |

A dead or moved link gets the Wayback Machine's snapshot closest to
`urldate` as its finding's `url`, when there is one.

### Claims

Claims are checked only when asked, because each one costs money.

- Every cited entry's citing sentences (up to 12, of up to 600 characters
  each) go to Claude together with the entry's abstract, from Semantic Scholar, Crossref
  or arXiv. When a sentence quotes text or gives a number, the full text
  goes too where it is open (arXiv's HTML), up to 300,000 characters.
- Claude answers per sentence `supported`, `partly`, `not_supported` or
  `unclear`, with a reason and the passage it relied on. Each answer is a
  `claim` finding with `source` `claude`, a warning for `not_supported` and
  info for the rest. A claim is never an error.
- A claim finding also has `path`, `line` and `verdict`. `value` is the
  sentence, `record_value` the passage and `message` the reason.
- An entry without an abstract is not sent. Its sentences become claim
  findings (info) with the verdict `no_source`.
- One request per entry, through the official `anthropic` SDK, with
  `REFERENCES_CLAUDE_MODEL` (`claude-opus-5-5`), effort `medium`, a JSON
  schema for the answer and the API's own fallback for refused requests
  (`fallbacks: "default"`). Four requests run at a time.
- The sentences and the source are data. The instructions say to judge
  only from the given text, never from what the model remembers.
- Without `ANTHROPIC_API_KEY`, claims are off. The report says so
  (`claims_enabled`) and a run with `claims: true` is 503.

### Statuses

Each entry has the first status that applies.

| Status | When |
| --- | --- |
| `error` | The work was retracted, even when the entry is checked. |
| `checked` | The checked mark matches. |
| `changed` | The checked mark does not match any more. |
| `error` | Any other error. |
| `warning` | A warning. |
| `pending` | No run has looked the entry up as it reads now. |
| `verified` | A record matched, or the link works. |
| `unverified` | The run could not look anything up. |

The References tab counts the entries that are `error` or `changed`.

### Runs

- One run per project at a time, in a thread of its own. `total` and `done`
  count the lookups and the claim checks of the run, for a progress bar.
- A run looks up only the fingerprints it has no result for, or a result
  older than a day, unless `refresh`. Claims are asked again only for
  entries whose fingerprint, sentences or source changed.
- Lookups go through one cache for all projects. Records are kept 7 days,
  links 1 day and failures 1 hour. Rows older than 30 days are removed when
  a run starts.
- Each service gets its own pace. Semantic Scholar and DBLP get one request
  a second, arXiv one every three seconds. A 429 or a server error is tried
  again twice, after a pause. A service that fails three times in a row is
  not asked again during that run, so an outage or a bot wall does not slow
  the whole run down. Requests carry
  `User-Agent: meertex/<version> (<BASE_URL>; mailto:<REFERENCES_MAILTO>)`,
  which puts them in Crossref's polite pool.
- **Links are fetched carefully,** because anyone who can edit a `.bib`
  chooses them. Only `http` and `https`. The host must resolve to public
  addresses only (not loopback, private, link-local, shared, multicast or
  reserved ones). That is checked again for every redirect (at most 5) and
  for the address the connection went to, before the body is read. At most
  1 MB is read, without a proxy from the environment.
- A run that was `running` when the server stopped is `failed` with "The
  server restarted during the check".

Rows:

- `reference_runs (id, project_id, status, claims, refresh, total, done, error, started_at, finished_at, created_by)`.
  `status` is `running`, `done` or `failed`.
- `reference_results (project_id, fingerprint, key, checked_at, record, findings, claims, claims_hash, claims_at)`,
  one per project and fingerprint.
- `reference_cache (key, ok, value, fetched_at)`, shared by all projects.

### Routes

```text
references {run: run | null, claims_enabled, network_enabled, files: [path],
            counts: {<status>: n}, problems, missing: [{key, path, line}],
            broken: [{path, line, key, message}], entries: [entry]}
entry      {key, type, path, line, end_line, fingerprint, fields: {name: value},
            status, mark: {date, by, hash, valid} | null, record | null,
            checked_at, findings: [finding], claims_at, cites: [{path, line}]}
record     {source, doi, url, type, title, authors: [string], year, venue,
            volume, number, pages, publisher}
run        {id, status, claims, refresh, total, done, error, started_at,
            finished_at, created_by: person | null}
```

- `GET /api/projects/{ref}/references` returns the report. Entries are in
  file order. `problems` is what the tab counts, the entries that are
  `error` or `changed` plus the broken ones. `missing` are `\cite` keys no
  entry has. `broken` are the entries that cannot be parsed (`key` is null
  when not even the key could be read). `checked_at` is when a run last looked the entry up as it
  reads now, null when none has. `claims_at` is when its claims were
  checked, null when they were not. Viewers too.
- `GET /api/projects/{ref}/references/summary` returns `{problems, total}`,
  for the tab's counter.
- `GET /api/projects/{ref}/references/problems` returns the errors and
  warnings as `[{level, path, line, message}]`, the shape of a compile's
  problems (§14), with the broken entries as errors, the missing keys as
  warnings on their `\cite` and the claims on their sentence.
- `GET /api/projects/{ref}/references/check` returns the newest run or
  null, `GET .../references/check/{id}` that run. `POST` with `{claims?, refresh?}` starts one and returns it (202),
  or returns the one that is running (200). Editors and the owner. 503
  when `REFERENCES_NETWORK` is off, or for claims without a key.
- `POST /api/projects/{ref}/references {source, key?, path?, dry_run?}` adds
  an entry and returns `{key, path, line, bibtex}` (201, or 200 without
  writing for `dry_run`).
  - `source` is a DOI (also as a doi.org link), an arXiv id or link, an
    ISBN, or any other link. A DOI is built from its CSL JSON, an arXiv id
    from DataCite's, an ISBN from Open Library, and a link from the page's
    `citation_*` meta tags (`citation_doi` turns it into a DOI), JSON-LD and
    Open Graph, as `@online` with today's `urldate`.
  - The key defaults to the first author's family name, the year and the
    first word of the title that is not a stop word, in lower case ASCII
    (`vaswani2017attention`), with `b`, `c` and on appended while it is
    taken. A given key that is taken is 422.
  - `path` defaults to the `.bib` file with the most entries, or a new
    `refs.bib` when there is none.
  - The entry is appended with a blank line before it. Title words with a
    capital after their first letter are kept in braces (`{GPU}`,
    `{BERT}`). Page ranges use `--`.
  - A source that names nothing is 404, a service that cannot be reached is 503.
- `POST /api/projects/{ref}/references/{key}/fields {field, value}` sets one
  field, replacing its value or appending it. It returns the entry.
- `POST /api/projects/{ref}/references/{key}/checked` marks the entry and
  `DELETE` unmarks it. Both return the entry.
- The writes need the editor role. A key no entry has is 404, a key that
  two entries have is 409.

### CLI

The commands are in `cli/meertex/cmd_refs.py`.

| Command | |
| --- | --- |
| `meertex refs <project>` | Every entry with its status and findings, the missing keys and the last run. Exits 1 when an entry is `error` or `changed`. |
| `meertex refs <project> check [--claims] [--refresh]` | Starts a run, shows its progress and then prints like `refs`. |
| `meertex refs <project> add <source> [--key K] [--path P] [--dry-run]` | Adds an entry and prints it. |
| `meertex refs <project> mark <key>`, `unmark <key>` | The checked mark. |
| `meertex refs <project> set <key> <field> <value>` | Sets one field. |

### In the web app

- The **References** tab (`/p/<name>/references`) sits between Reviews and
  Settings, with the count of problems.
- At the top are the last run (when, by whom, the progress while it runs),
  "Check references", "Check claims" (only with `claims_enabled`, and it
  says that it costs money) and "Add a reference". Without
  `network_enabled` a note says so instead of the three buttons, because
  adding needs the lookups too.
- A filter by status and a search box over keys, titles and authors. Its
  "Needs a look" shows the `error`, `changed` and `warning` entries.
- One row per entry with its status, key, title, first authors, year and
  the number of findings. A row opens to its findings, each with "Take this
  value" where there is a record value, the entry's fields next to the
  record's, the claims with their sentences, a link to the entry's line in
  the editor, and "Mark as checked" or "Remove the mark".
- The missing keys are listed with links to their `\cite`.
- The editor shows the reference errors and warnings in its problems list
  and on their lines. It reads them when it opens and after every compile.
