# meertex

The command line tool of [meertex](https://meertex.com), a LaTeX editor for the
whole life of a paper. It keeps a folder on your computer in
step with the live document on the server, so you write in VS Code, vim or with
Claude Code while your coauthors see every save within a second. It also reads
and changes everything else a paper has: its venue and deadlines, its versions,
its reviews and its references.

## Install

It needs Python 3.11 or newer.

```bash
pipx install meertex          # or: uv tool install meertex
pipx upgrade meertex          # every change on main is a new release
```

## Sign in and write

```bash
meertex login --server meertex.com      # mails you a sign-in code
meertex projects                        # your papers with their next deadline
meertex checkout 2027-icse-green-llm    # the paper in ~/papers/2027-icse-green-llm
meertex sync                            # keeps every checkout in step while it runs
meertex service install                 # Linux and macOS: runs meertex sync in the background
meertex service status                  # does it run, and its last log lines (-f follows)
```

`--server` takes the address of your own meertex server just as well.

## Everything else

```bash
meertex show 2027-icse-green-llm                     # venue, dates, questions, versions
meertex diff 2027-icse-green-llm submission --word   # what changed since the submission
meertex compile 2027-icse-green-llm                  # the PDF and the LaTeX problems
meertex threads 2027-icse-green-llm                  # open review comments with their text
meertex refs 2027-icse-green-llm check               # look every reference up
```

Every command answers in JSON with `--json`, for scripts and agents. `meertex
--help` lists them all, and
[docs/DESIGN.md](https://github.com/ribalba/meertex/blob/main/docs/DESIGN.md)
describes each one.

## License

AGPL-3.0-or-later. The source is at
[github.com/ribalba/meertex](https://github.com/ribalba/meertex).
