# Deploying meertex with Coolify

This deploys meertex, Postgres, the server and the compile service, as one
Coolify resource from [`docker-compose.coolify.yml`](docker-compose.coolify.yml).
Coolify's Traefik terminates TLS in front of the server and passes the sync
WebSockets through. The database and the compile service are not reachable
from outside the host at all.

## What you need

| | |
| --- | --- |
| **Host** | 1 GB RAM is plenty for the server and Postgres, compiling adds up to `COMPILE_MEMORY` (2 GB), and the spelling checker up to `LANGUAGETOOL_MEMORY` (1.5 GB). Disk for the papers' histories and figures (`MAX_BLOB_BYTES`, 50 MB, is the largest single file), and 6 GB for the TeX Live image ([Compiling](#compiling)). |
| **Coolify** | v4. The compose file uses its magic variables (`SERVICE_PASSWORD_*`) and required variables (`${BASE_URL:?}`). |
| **DNS** | An A record for the app's hostname pointing at the Coolify host. |

## 1. Create the resource

1. **+ New → Docker Compose** (under Applications), pointing at this repository,
   branch `main`.
2. Set **Docker Compose Location** to `/docker-compose.coolify.yml`.
3. Save. Coolify parses the file and pre-fills the Environment Variables tab
   with everything it references. **Do not deploy yet.**

## 2. Set the environment variables

| Variable | |
| --- | --- |
| `BASE_URL` | **Required**, e.g. `https://meertex.com`. Coolify refuses the deploy while it is empty. Sign-in links, invite mails and the project URLs the API hands out are built from it. |
| `SERVICE_PASSWORD_POSTGRES` | Leave it alone. Coolify generates it once and reuses it on every redeploy. Postgres only reads it when it creates the database volume. |
| `SERVICE_PASSWORD_64_MEERTEX` | Leave it alone. The `SECRET_KEY` that signs login codes. Changing it only invalidates sign-in links and codes still in flight. |
| `SERVICE_PASSWORD_64_COMPILE` | Leave it alone. The token `web` shows the compile service. Both read the same variable. |
| `COMPILE_TIMEOUT_SECONDS`, `COMPILE_KEEP_JOBS`, `COMPILE_KEEP_WORKSPACES`, `COMPILE_KEEP_LIVE_PDFS`, `COMPILE_WORKERS`, `COMPILE_MEMORY`, `COMPILE_CPUS` | Optional. Default 90 seconds per compile, 30 jobs kept for SyncTeX, 20 papers' editor workspaces, the PDFs of a paper's 20 newest live compiles, 2 compiles at once, 2 GB of memory and 2 CPUs for the compile container. |
| `COMPILE_SANDBOX` | Optional, default `auto`. The sandbox every compile job runs in ([Compiling](#compiling)). `on` makes the compile service refuse to start on a kernel without Landlock, `off` switches the sandbox off. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `EMAIL_FROM` | Needed before anyone but you can sign in, for invites, and for the reminder mails. Without `SMTP_HOST` the sign-in link and code are printed to the `web` container's log instead, which is how you sign in the first time (step 5). |
| `GRAMMAR_URL`, `GRAMMAR_TIMEOUT_SECONDS`, `GRAMMAR_ALLOW_PUBLIC_HOSTS`, `GRAMMAR_MAX_CONCURRENT`, `LANGUAGETOOL_MEMORY` | Optional. The editor's spelling and grammar check ([Spelling and grammar](#spelling-and-grammar)). Empty `GRAMMAR_URL` means the bundled `languagetool` service. Default 20 seconds per check, public hosts refused, 3 requests at once, 1.5 GB of memory for the container. |
| `ANTHROPIC_API_KEY`, `ASSISTANT_CLAUDE_MODEL`, `ASSISTANT_DAILY_PER_ACCOUNT`, `ASSISTANT_DAILY_TOTAL`, `REFERENCES_CLAUDE_MODEL` | Optional. An Anthropic API key turns on what asks Claude: the new-paper assistant and the claim check of the references ([The assistant](#the-assistant)). Without a key both are off. |
| `NEW_USER_MAIL_TO` | Optional. One mail to this address for every new account, at its first sign-in, with who invited the person or that they signed up on their own ([docs/DESIGN.md](docs/DESIGN.md) §2, Accounts). Empty mails nobody. |
| `EXAMPLE_PAPER` | Optional, default `true`. Every new account starts with an example paper at its first sign-in ([docs/DESIGN.md](docs/DESIGN.md) §19). `false` makes none. |
| `INSTANCE_ADMINS` | Optional. Email addresses, separated by commas, whose accounts open the instance dashboard at `/admin` from the user menu ([docs/DESIGN.md](docs/DESIGN.md) §18). Empty means nobody. |
| `REMINDERS_ENABLED` | Optional, default `true`. The daily reminder mails before deadlines ([docs/DESIGN.md](docs/DESIGN.md) §5, Reminders). They need SMTP to reach anyone. `false` turns them off. |
| `MAX_TEXT_BYTES`, `MAX_BLOB_BYTES`, `MAX_SYNC_MESSAGE_BYTES` | Default 2 MB, 50 MB and 64 MB. The last one has to stay below the `--ws-max-size` in the Dockerfile. |
| `SNAPSHOT_IDLE_SECONDS`, `SNAPSHOT_MAX_SECONDS`, `ROOM_IDLE_SECONDS` | Default 60, 600 and 300. When the git history gets a commit, and when an unused document leaves memory. |
| `LOGIN_*`, `SESSION_TTL_MINUTES`, `VERIFY_RATE_MAX` | Optional. [`.env.example`](.env.example) explains each. |

## 3. Give the app its domain

On the **`web`** service, set **Domains** to `https://meertex.com:8000`.
The `:8000` tells Coolify which container port to route to. Coolify writes the
Traefik labels and gets the certificate over Let's Encrypt.

Do **not** add `ports:` to any service. Postgres has no business on the
internet, and the server trusts `X-Forwarded-For` from whoever can reach port
8000 directly.

## 4. Deploy

The image builds from this repository, Postgres comes up healthy, and the server
creates its schema on first start and applies any new migrations on every later
one (`app/migrations.py`). The image has a healthcheck on `/healthz`.

## 5. Sign in the first time

Open the app and enter your address. Without SMTP the link and the code are in
the log, under **`web` → Logs** in Coolify, or on the host:

```bash
docker ps --filter name=web- --format '{{.Names}}'
docker logs <that container> 2>&1 | grep -A16 '\[EMAIL\]' | tail -20
```

Then, on your laptop:

```bash
make cli                                   # pipx install ./cli
meertex login --server https://meertex.com
meertex service install                    # keeps ~/papers in sync in the background
```

## Compiling

The `compile` service is TeX Live 2025 (the full scheme) with a small HTTP
service in front ([compile/README.md](compile/README.md)). `web` sends it the
files of a project and keeps the PDF it gets back. It has no domain and no
ports, and every request carries the token.

| | |
| --- | --- |
| **Image** | Built from `compile/Dockerfile` on `texlive/texlive:TL2025-historic`, pinned by digest. The base is a 2.6 GB download that unpacks to 5.5 GB, so the first deploy takes a few minutes longer and needs about 10 GB of free disk while it builds. Later deploys reuse it, as long as nothing prunes the image. |
| **Limits** | `COMPILE_MEMORY` (2 GB), `COMPILE_CPUS` (2) and 256 processes for the container. `COMPILE_WORKERS` (2) compiles run at once, and each one is stopped after `COMPILE_TIMEOUT_SECONDS` (90) with all its processes. One project compiles one thing at a time. |
| **Isolation** | An unprivileged user, a read-only root file system, no capabilities. Every job runs in a sandbox of the kernel's (Landlock), where it reads TeX Live and writes to its own paper's files, and nothing else of the container is open to it. latexmk runs without rc files, TeX without shell escape and in kpathsea's paranoid mode. |
| **Secrets** | Coolify hands every variable of the resource to every service, so the `compile` container holds the passwords of the database and the mail server, and an API key if you set one. A document gets none of them. A job's environment holds only `PATH`, the locale and TeX's settings, and the sandbox closes `/proc`, where the container's environment can be read. |
| **Sandbox** | It needs Landlock in the host's kernel, which Linux has from 5.13 on. Ubuntu switches it on from 22.04 on, and `cat /sys/kernel/security/lsm` on the host lists `landlock` when it is on. The `compile` log starts with `jobs in a sandbox (Landlock N)`. If it says `jobs WITHOUT a sandbox` the kernel has none. A lualatex document can then read every paper that is in the container and the secrets above, so update the host or keep sign-up to people you trust. `COMPILE_SANDBOX=on` makes the service refuse to start without the sandbox. |
| **Network** | Coolify puts every service of the resource on the resource's network, which reaches the internet. Nothing in a job opens a connection, though. The sandbox closes TCP to a job (Landlock 4, Linux 6.7), TeX runs without shell escape, and a guard stops biber before it fetches a `.bib` file named by URL. `docker-compose.yml` on a laptop puts the service on an internal network with no way out. |
| **Jobs** | The `compile-jobs` volume holds the newest `COMPILE_KEEP_JOBS` job folders, for SyncTeX, and the `COMPILE_KEEP_WORKSPACES` workspaces of the papers edited last. A workspace holds the paper's files and its font caches, up to 130 MB when the paper uses biber, which unpacks itself there. It needs no backup. The PDFs themselves are stored by `web`, with the project. |
| **First compiles** | A paper's first compile with biber takes about 3 seconds longer, while biber unpacks itself into the paper's workspace. A compile outside the editor (a version, a diff, the CLI) pays that every time, because it keeps no caches. |
| **Without it** | The compile routes answer 503 and say why. Everything else works. |

## Spelling and grammar

The editor underlines spelling and grammar mistakes in `.tex`, `.md` and `.txt` files. `web` asks the
`languagetool` service, LanguageTool 6.8 in the community image `erikvl87/languagetool`
([docs/DESIGN.md](docs/DESIGN.md) §17).

| | |
| --- | --- |
| **Image** | About 450 MB, pulled on the first deploy. The first check in a language loads it, which takes a few seconds. |
| **Memory** | Up to 1.3 GB resident once two languages are loaded. `LANGUAGETOOL_MEMORY` (1.5 GB) is the container's limit, and the JVM heap is capped at 768 MB inside it. |
| **Privacy** | Unpublished papers go to the checker, so it has no domain and no ports, and `web` refuses a `GRAMMAR_URL` with a public address unless `GRAMMAR_ALLOW_PUBLIC_HOSTS` is `true`. Nothing logs the text. |
| **Load** | `web` caches results per paragraph and only sends the paragraphs that changed. At most `GRAMMAR_MAX_CONCURRENT` (3) requests are with the checker at once. |
| **Without it** | While the container is stopped or starting, checks answer 503 and the editor says so on its Spellcheck button. Everything else works. |

## The assistant

"Fill in from the call for papers" on the new-paper form asks Claude to read
the address somebody pastes. Claude fills in the venue, the deadlines and a
`main.tex` in the venue's format, and the person checks the form before the
paper is created ([docs/DESIGN.md](docs/DESIGN.md) §21).

| | |
| --- | --- |
| **Key** | `ANTHROPIC_API_KEY`, an API key from the Anthropic Console. Mark it as a secret in Coolify. The same key turns on the claim check of the references (§16, Claims), which every editor of a paper can then start and which asks Claude once per cited entry. |
| **Model** | `ASSISTANT_CLAUDE_MODEL`, default `claude-opus-5-5`. Any Claude from 4.6 on works, for example `claude-sonnet-5-5` or `claude-haiku-5-5`, which cost less. The claim check has its own `REFERENCES_CLAUDE_MODEL`. |
| **Cost** | One request reads up to 8 pages, and Claude gets at most 40,000 tokens of each. What a request really took is in the `web` log, one line per request with the model, the pages read and the tokens in and out. |
| **Limits** | `ASSISTANT_DAILY_PER_ACCOUNT` (10) requests per account and `ASSISTANT_DAILY_TOTAL` (200) for the whole server, each within 24 hours. Anyone can sign up, so the second one caps what a day can cost. `0` in either turns the assistant off. At most 4 requests run at once. |
| **Web fetch** | Claude reads the pages with Anthropic's web fetch tool, so the request leaves from Anthropic, not from this server. An organization admin can turn web fetch off on the Capabilities page of the Anthropic Console. The assistant then answers with an error that says so. |
| **What Anthropic gets** | The address, the track when one was typed, and today's date. Nothing of a paper. |
| **Without it** | The form has no assistant box and everything else works. |

## Operating it

| | |
| --- | --- |
| **One worker** | The server runs one uvicorn process with one worker, on purpose. Each open paper is held in memory as a room, and a second worker would hold a second copy. Do not scale the `web` service. |
| **Backups** | Two volumes: `pg-data` (accounts, projects, dates, versions and the live documents) and `meertex-data` (every project's git history and binary files). Back them up together. |
| **Upgrades** | Redeploy. Open documents are written out on shutdown, and the sync clients reconnect on their own and send what they collected meanwhile. |
| **Postgres major upgrade** | The volume is mounted at `/var/lib/postgresql`, not `.../data`, which keeps `pg_upgrade --link` available later. |
| **Sign-in mails** | Without SMTP they are in the `web` log. With SMTP and none arriving, the log says why. |
| **Reminder mails** | `web` looks every five minutes for people whose own hour has come (Settings, Reminders) and mails them one digest. What was sent is kept in the database, so a redeploy sends nothing twice. Without SMTP the digests are in the `web` log. |

## Local development is unaffected

`docker-compose.yml` is untouched by any of this. `make up` and `make dev` work
as [README.md](README.md) describes.
