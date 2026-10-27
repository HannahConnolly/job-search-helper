# job-search-helper

A LoopCV-style job search loop for your own use. It re-runs saved searches across job boards and
company career pages, scores every new posting against your resume and interview notes with Claude,
and writes tailored prep packets for the roles worth applying to.

## Your documents

`jobhelper sync` (and every `jobhelper run`) downloads two documents into `data/`:

- **resume**: your resume, as text.
- **behavioral**: your interview stories and answers.

Set `resume_doc` and `behavioral_doc` in the `[profile]` section of `config.toml` to a Google Docs
link (shared as "Anyone with the link can view"), a doc id, or a path to a local text file.

## How it works

1. **Sync**: both docs are exported as text into `data/`.
2. **Search**: each `[[loops]]` entry in `config.toml` is a saved search, run on Indeed and LinkedIn
   through [JobSpy](https://github.com/speedyapply/JobSpy). The `[companies]` boards are read through
   the public Greenhouse, Lever and Ashby APIs (full descriptions, direct apply links). With a LoopCV
   API key, LoopCV loops are added as another source.
3. **Filter**: titles and locations that don't fit (`[filters]`) are dropped, and the same job seen
   on several boards is stored once.
4. **Score**: Claude reads each new posting alongside the resume, interview notes and
   `preferences`, and returns a 0-100 fit score with strengths, gaps and dealbreakers. Postings that
   mention the most resume skills are scored first.
5. **Digest**: `output/digests/` gets a summary of the new matches.
6. **Prep**: for a job worth applying to, `jobhelper prep ID` writes tailored resume bullets, a cover
   letter, likely interview questions mapped to your stories, and an outreach note.
7. **Track**: `jobhelper mark ID applied`, `interviewing`, `offer`, and so on.

Nothing is submitted automatically; applying stays manual.

## Setup

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env                  # then add ANTHROPIC_API_KEY
cp config.example.toml config.toml    # then set your documents, searches and filters
```

`.env` and `config.toml` hold your keys and personal settings and are gitignored.

Run commands as `.venv/bin/jobhelper ...`, or activate the venv first
(`source .venv/bin/activate.fish` in fish).

## Commands

| Command | What it does |
|---|---|
| `jobhelper run` | The whole loop: sync, search, score new jobs, write a digest |
| `jobhelper list` | Best new and saved matches (score at least `min_score`) |
| `jobhelper list --all`, `--status applied` | Every job, or one pipeline stage |
| `jobhelper show ID` | Details, Claude's assessment and the description |
| `jobhelper prep ID` | Tailored packet in `output/prep/` (marks the job saved) |
| `jobhelper open ID` | Open the apply link in the browser |
| `jobhelper mark ID applied -n "referred by ..."` | Update status, with an optional dated note |
| `jobhelper review` | Resume critique plus a tightened STAR version of every behavioral answer, in `output/review-DATE.md` |
| `jobhelper search`, `jobhelper rank` | The search and scoring steps on their own (`rank --limit 200`, `rank --rescore`) |

## Configuration

Everything lives in `config.toml` (start from `config.example.toml`, which has comments):

- `[profile]`: where the documents come from, plus `preferences` that steer scoring and writing.
- `[filters]`: title words to require or exclude (senior roles are excluded by default), and which
  locations count. Filters apply when a job is found, so changes affect later searches.
- `[search]` and `[[loops]]`: the saved searches and which boards they run on.
- `[companies]`: company boards to watch. The slug is in the careers URL
  (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`).
- `[llm]`: models, effort, the per-run scoring cap and the score cutoff.
- `[loopcv]`: settings for the optional LoopCV source.

## Costs

Scoring uses `claude-opus-5-5` at low effort, roughly 2-3 cents per job. A prep packet is roughly
10-25 cents and a full review 30-60 cents. Every command prints its token usage and an estimated
cost. `max_jobs_scored_per_run` (default 60) caps each run; the rest wait for the next run or
`jobhelper rank`. Setting `score_model = "claude-haiku-5-5"` makes scoring about 40x cheaper, at
some cost in judgment.

## Running it every morning

A systemd user timer:

```ini
# ~/.config/systemd/user/jobhelper.service
[Unit]
Description=job-search-helper daily loop

[Service]
Type=oneshot
WorkingDirectory=%h/Projects/job-search-helper
ExecStart=%h/Projects/job-search-helper/.venv/bin/jobhelper run
```

```ini
# ~/.config/systemd/user/jobhelper.timer
[Timer]
OnCalendar=Mon..Fri 08:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `systemctl --user enable --now jobhelper.timer`; the output goes to
`journalctl --user -u jobhelper`, and the digest to `output/digests/`.

## Notes

- JobSpy reads the boards' public search pages. It suits a personal search at modest volume, but
  LinkedIn rate-limits heavy use from one IP, and a scraper can break when a site changes; the
  search summary shows when one fails. The company boards use official APIs.
- LoopCV's API (https://api-docs.loopcv.com) is a B2B product and needs a key from LoopCV. The
  integration follows their docs but hasn't been run against a live key.
- Claude is told to use only facts from your documents and to leave `[add: ...]` placeholders instead
  of inventing details. Still read everything before sending it.
- `data/` (synced documents, job database), `output/` (packets, digests, reviews), `.env` and
  `config.toml` are gitignored, so nothing personal is committed.

## Development

```sh
.venv/bin/pytest
```
