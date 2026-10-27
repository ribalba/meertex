---
name: meertex
description: Read and (only when asked) change the user's papers in meertex, their own LaTeX paper workspace, through the `meertex` CLI or its token API. Use whenever the user asks about their papers, deadlines, submissions or acceptances ("when is the EuroSys deadline", "which papers got accepted", "what is due this month"), wants to work on a paper in ~/papers, or wants to start, version or resubmit a paper. Also for reviews: reading and working through review comments on a paper ("what did Arne say about the intro", "fix the open review comments"), and reviewing a paper in meertex when asked. And for references: checking a paper's .bib for wrong or made-up citations ("are my references ok", "check the bibliography") and adding entries from a DOI, an arXiv id or a link.
---

# The user's papers in meertex

meertex is the user's own paper workspace (repo `/home/didi/code/meertex`).
It runs at https://meertex.com on the user's server under Coolify, like the
other meer* apps (see `coolify.md`).
Every paper is a **project** with its venue (conference, year, track, links),
its **dates** (abstract, submission, rebuttal, notification, camera-ready, the
conference itself), its **outcome** (accepted, rejected, revision, withdrawn,
not submitted) and its fixed **versions** (submission, rebuttal, Revision N,
camera-ready). The files are a live document that coauthors edit in the browser
while the user's checkouts in `~/papers/<name>` stay in step through
`meertex sync`.

`docs/DESIGN.md` in the repo is the contract for every field and route.

## Rules

- **Reading is free.** List, show, diff and read files as much as the question needs.
- **Change things only when the user asks.** That covers creating projects,
  editing fields or dates, answering questions, creating versions and
  inviting people. Every change is visible to every coauthor at once.
- **Never answer a lifecycle question for the user** ("Did you hand it in?",
  "Was it accepted?"). Tell them it is waiting and what the answers would do.
- **Versions are the record of what was handed in.** Never delete one, and
  never create a `submission` or `camera_ready` version unless the user says it
  was handed in.
- **Deleting a project deletes its history and files for every member.** Only
  on an explicit request, and confirm the name first.
- **Review threads are the reviewers' and the authors' conversation.** Never
  reply, resolve, apply a suggestion, accept, reopen, comment or submit a
  review unless the user asks for it. Each of these is in the user's name,
  shows at once and mails the people in the thread.
- **Never print the API token.**

## Connecting

The CLI reads the server address from `~/.meertex/config.toml` and the token
from `~/.meertex/token`. If a command says it is not logged in, ask the user to
run `meertex login --server https://meertex.com`, which mails them a code. If
the server does not answer, tell the user rather than working around it.

```bash
meertex status --json          # checkouts and whether sync runs
meertex projects               # the list with countdowns
```

For anything the CLI does not cover, the token API takes the same token:

```bash
B=$(sed -n 's/^server *= *"\(.*\)"/\1/p' ~/.meertex/config.toml)
H="Authorization: Bearer $(cat ~/.meertex/token)"
curl -s -H "$H" "$B/api/projects?outcome=accepted"
```

| Status | Meaning |
| --- | --- |
| 401 | The token is wrong or rotated. Ask the user to run `meertex login`. |
| 403 | The user's role on that project is too low for the change. |
| 404 | No such project, or the user is not a member. |
| 409 | A name is ambiguous or taken, or a folder is not empty. `detail` says which. |
| 422 | The request was refused. The reason is in `detail`. Nothing was written. |

## Reading

```bash
meertex projects --json                       # summaries, next date first
meertex projects --outcome accepted           # all accepted papers
meertex projects --outcome none               # everything still under review or in writing
meertex projects --q energy --all             # search, archived included
meertex show 2027-icse-green-llm              # fields, dates, questions, versions
meertex questions                             # lifecycle questions waiting for the user
meertex versions 2027-icse-green-llm
meertex diff 2027-icse-green-llm submission --word   # what changed since the submission
```

`--json` on any command gives the raw shapes of DESIGN.md §11.

## Working on a paper

The checkout is `~/papers/<name>`. Edit files there as in any folder.

- If `meertex status` shows the sync service running, every save reaches the
  server within a second and coauthors' edits arrive in the files. Before
  editing a file that someone else may be working on, read it again rather
  than relying on an earlier read.
- If it is not running, run `meertex sync --once` in the checkout before you
  start and again when you are done.
- Build outputs (`*.aux`, `*.log`, the main PDF, ...) and `.git`, `.claude`,
  `.venv` are never synced. `.meertexignore` (gitignore syntax) adds more.
- Files deleted by someone else are moved to `.meertex/trash/`, never lost.
- **Holding.** `meertex hold .` keeps the checkout's changes local while
  coauthors' edits still arrive, and `meertex release .` sends them all in one
  go. It suits a large refactor that would leave the paper broken for a while.
  Hold before it and release when the paper compiles again. Only do this when
  the user wants it, because coauthors do not see any of the work until the
  release. `meertex status` shows held checkouts and how many files wait.
  `meertex sync --once` on a held checkout only pulls, and says so.

## Changing things (when asked)

```bash
meertex new --from-json new.json --checkout --json    # a new paper (the meertex-new-paper skill does the CFP part)
meertex new --from-json new.json --zip kit.zip --checkout --json   # starting from a publisher's author kit
meertex fork <project> --from-json venue.json --checkout   # resubmit to another venue
meertex set <project> outcome=accepted doi=10.1145/...
meertex date <project> set submission 2027-01-22@AoE  # a deadline moved
meertex answer <project> submission yes --at deadline # the user said it was handed in
meertex version <project> camera_ready --pdf main.pdf # freeze a version, attach the PDF
meertex new --planned --conference ICT4S --year 2027 --date submission=2026-12-10@AoE   # a venue without a topic yet
meertex start <plan> --title "..." --starter basic    # the topic is clear: the plan becomes a paper
```

## Planning

`meertex timeline` answers "what do I have to do when": when to start
writing which paper (its submission minus the user's writing time), what is
due, and where writing windows overlap. Use it when the user asks about their
workload or deadlines across papers. `meertex timeline --writing-days N`
changes the user's usual writing time, so only pass it when asked.
`meertex set <project> writing_days=N` sets one paper's own time.
Plans (`planned: true`) are venues the user wants to submit to before the
topic is clear. They ask no questions and turn into papers with `meertex start`.

`meertex reminders` answers "what is coming up" in short. It prints what the
user's next daily reminder mail would say: the deadlines within their reminder
days (14, 3 and 1 days before and the day itself by default), papers whose
writing time starts now, and the questions waiting. It leaves out muted and
archived papers and papers where the user is only a reviewer or viewer, so use
`meertex timeline` for the whole picture. `--json` gives the items with
`days_left`. Change the reminder settings (`PATCH /api/auth/me {reminders,
reminder_days, reminder_hour}`) or mute a paper
(`PUT /api/projects/{ref}/reminders {muted}`) only when the user asks.

## References

`meertex refs <project>` prints the reference check of DESIGN.md §16: every
`.bib` entry that needs a look, with its status (`error`, `changed`,
`warning`, `pending`, `unverified`, `verified`, `checked`) and its findings,
plus the `\cite` keys without an entry and the entries that do not parse.
`--all` lists the fine ones too, `--json` gives the raw report.

```bash
meertex refs <project> check                  # look every entry up and print the report
meertex refs <project> check --claims         # also ask Claude whether the sources support the citing sentences
meertex refs <project> add 10.1145/3290605.3300857 --dry-run   # an entry from a DOI, arXiv id, ISBN or link
meertex refs <project> set <key> pages 1--13  # take a value, usually a finding's "record"
meertex refs <project> mark <key>             # the user checked this entry by hand
```

- Reading the report and running `check` are fine whenever the user works on
  the references. `check --claims` costs money for every cited entry, so run
  it only when the user asks for it.
- A finding with a `record:` line has the publisher's value. Taking it with
  `set`, or editing the `.bib` in the checkout, changes the paper, so only do
  it when the user asks.
- **Never mark an entry as checked on your own.** `mark` writes
  `meertex-checked = {date, name, hash}` into the entry and says that a
  person checked it by hand. Only the user can say that. An entry you or
  anyone else edits afterwards turns `changed`.
- Findings are hints. `not_found` and `doi_mismatch` mean "look at it", not
  "delete it". Tell the user what was found and let them decide.

## Reviews

meertex has GitHub-style reviews for papers. A reviewer starts a review on a
fixed state (`HEAD`, a version or the current text), comments on lines or spans
of the text, optionally with a **suggestion** (replacement text), and submits
the review with a verdict (`approve`, `comment`, `changes_requested`). The
authors reply, change the text and **resolve** each thread. The reviewer then
**accepts** the result or **reopens** the thread. A comment can also stand on
its own, outside any review. Every thread knows where its text is now, even
after edits, renames and moves.

### Reading the comments

```bash
meertex threads                          # every open thread in all projects
meertex threads <project>                # one project, in file order
meertex threads <project> --state all    # resolved and accepted ones too
meertex threads <project> --json         # the raw shapes of DESIGN.md §13
meertex thread <project> a1b2c3d4        # one thread, by the first 8 characters of its id
meertex reviews <project>                # the reviews, their verdicts and summaries
```

Each thread prints as its short id, its state, who started it and
`path:line_start-line_end`, which is where the text is **now** in the live text
(the checkout, once synced). Then come the quoted text the comment was made on,
the current text under `now` when it changed since, the whole conversation, and
every suggestion as a small diff (`-` the current text, `+` the replacement).
`outdated` means the text was deleted. Then the place shown is where it was.

### Working through comments (when asked)

1. Sync the checkout first (`meertex sync --once` when the service is not
   running), so the line numbers match the files.
2. Read the file around the thread's lines, fix the text in the checkout and
   sync again.
3. When the user wants the thread answered, reply with what you changed, in a
   sentence or two, as in
   `meertex reply <project> <id> "Now names GPU and CPU energy, see line 42."`
4. Resolve only when the user asked you to. Resolving snapshots the files and
   records how the text changed since the comment, which the reviewer sees
   next to their comment. Use `meertex resolve <project> <id> --note "..."`.
5. A suggestion the user agreed to is applied with
   `meertex apply <project> <id>` (the newest one, or `--comment <id>`). It
   writes the replacement into the live text, so the checkout gets it with the
   next sync, and resolves the thread. Prefer it over editing by hand, so the
   thread records what happened. It refuses a thread whose text is gone.
6. Accepting is the reviewer's step (`meertex accept`). Only the person who
   started the thread or the owner can do it. Reopen with
   `meertex reopen <project> <id> --note "..."` when the user says a thread is
   not done.

Resolving and applying need the editor or owner role. Commenting, replying
and reopening work for reviewers too. Too low a role is 403.

### Reviewing a paper (when asked)

```bash
meertex review <project> start                         # on HEAD, or --ref submission, --ref live
meertex comment <project> main.tex:42-44 "This claim needs a citation." --review
meertex comment <project> sections/eval.tex:57 "Typo." --suggestion "the energy consumption" --review
meertex review <project> submit --verdict changes_requested --summary "Two things before the deadline."
meertex review <project> discard                       # throw the pending review away
```

- Line numbers are those of the live text. Read the file in the synced
  checkout (or with `curl` through the token API) before commenting.
  `meertex comment` refuses lines that do not exist.
- A suggestion replaces the whole lines given, without their final newline.
  Write the complete replacement for those lines.
- Long comments, notes and summaries read from stdin with `-`, as in
  `meertex comment <project> main.tex:12 - --review <<'EOF'` ... `EOF`.
- Comments with `--review` stay invisible until the review is submitted.
  Submitting mails the owner and the editors, so show the user the comments,
  the verdict and the summary first and submit only when they say so.
- Without `--review` a comment is visible at once and mails the owner and the
  editors.
