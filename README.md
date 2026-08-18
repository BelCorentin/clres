# clres

Browse and resume your Claude Code conversations from a tiny terminal picker.

Each conversation gets an emoji (keyword-based, stable), a title (haiku-
generated from the whole conversation once indexed, else a live tail-goal or
the raw first prompt), the project it belongs to, and its age. Hit Enter to
resume it with `claude --resume` from its original working directory.

```
 clres · 19/1178 🔖focus · last 1d    ↑↓ · ⏎ resume · [ ] days · p proj · m 🔖 · c focus · / search · a all · ? · q
 🖥️ ✨ Terminal browser for Claude Code conversations   my-tools          2m
 🐛 ✨ Fix flaky integration test on CI                 backend           3d
 📚 ✨ Sphinx docs landing page refresh                 docs-site         5d
 ...
```

## Install (shell alias)

```bash
git clone https://github.com/BelCorentin/clres ~/git/clres
~/git/clres/install.sh   # adds `alias clres=...` to ~/.zshrc
```

## Install (Claude Code plugin)

Adds a `/clres` slash command that lists conversations inside a session:

```bash
claude plugin marketplace add ~/git/clres
claude plugin install clres@clres
```

## Usage

| invocation           | behavior                                                          |
|----------------------|-------------------------------------------------------------------|
| `clres`              | interactive curses picker (last 1 day)                            |
| `clres -d N`         | `--days N` — look back N days; `0` = no time limit                |
| `clres --all`        | include tiny + headless + 🤖 subagent transcripts, no time limit   |
| `clres --index`      | haiku-title every untitled/`~live`-only real conversation         |
| `clres --reindex`    | also re-title/re-summarize convos that already have a ✨ title     |
| `clres --summarize`  | haiku-summarize every untitled/`~live`-only real conversation      |
| `clres --list`       | plain table (also used when piped)                                |
| `clres --json`       | machine-readable dump                                             |
| `clres --help`       | flag reference                                                    |
| `/clres`             | list inside a Claude Code session (plugin)                        |

Unknown flags are rejected with a usage message and exit 2 (argparse), rather
than silently opening the picker.

`--days` and `--all` compose: an explicit `--days` always wins, so
`clres --all -d 7` means "everything, including subagents, from the last week".
🔖-flagged sessions ignore the time limit entirely and are always listed.

`--json` is **unfiltered by default** (back-compat: the whole scan, subagents
included). Passing `--days` or `--all` makes it apply the same selection the
list/picker uses — note that `--json --days 0` therefore returns *fewer* rows
than a bare `--json`, because "no time limit" still hides tiny/headless rows.

### Keys

- `↑/↓` or `j/k` — move · `g/G` — top/bottom · PgUp/PgDn — page
- `[` / `]` (or `-` / `+`) — shrink / grow the lookback window
- `/` — search (title + project + summary), `Enter` keeps filter, `Esc` cancels
- `Enter` — resume · `s` — summary popup · `t` — re-title · `q` — quit
- `m` — flag/unflag 🔖 **"might come back"** on the highlighted session
- `c` — toggle the focused view (🔖-flagged + window ⇄ everything)
- `p` — cycle the project filter · `a` — show hidden rows too
- `?` — key help popup

The window ladder is **1d → 3d → 7d → 14d → 30d → all**; a custom `--days N` is
spliced into it for the session. Stepping it also re-enables the focused view,
since the window only applies there. The header shows the active window
(`clres · 8/1178 🔖focus · last 1d`).

Search accepts any unicode codepoint (`é`, `ç`, `漢`) — clres reads keys with
`curses.get_wch()` and falls back to `getch()` on curses builds without it.

Each row shows the session's git branch (`⎇ branch`, `@worktree` if a linked
worktree) and a 🔖 when it's flagged to come back to.

### What gets shown by default

The picker opens on the **focused view**: sessions you flagged 🔖 to come back
to (via `m`), plus anything touched in the last **1 day** (`--days` / `[` `]` to
change) — so active work is never hidden. `c` widens it to everything; `a` (or
`--all`) additionally reveals hidden rows, dimmed. The 🔖 flag is a marker file
at `~/.claude/comeback/<session-id>`, so it persists across runs and machines.

If a window ends up empty, clres does not blank the screen — it keeps the wider
list and says so in the status bar.

**Hidden rows** are the tiny conversations (title under `CLRES_MIN_TITLE`
chars, e.g. a bare `/model`), the headless ones (statusline bots, sdk calls),
and 🤖 **subagent transcripts** — the `Task`-tool side conversations stored at
`~/.claude/projects/<proj>/<sid>/subagents/agent-*.jsonl`. Subagent rows are
marked with a 🤖 icon and an `agent:` title prefix. Resuming one opens its
**parent** session, since an agent transcript is not independently resumable.
They are also excluded from `--index` / `--summarize`.

Titles fall back to the live session-state goal (`~/.claude/goals/<sid>.json`,
written by the `session-state.sh` hook, marked `~` in the list) over the raw
first prompt when no ✨ title is cached yet. That goal is summarized from only
the transcript **tail** — it's meant for "what's happening right now" in the
statusline/ntfy push — so it tracks the last few turns, not necessarily the
conversation's actual topic. `~` titles stay eligible for `--index`/`t` so they
get upgraded to a real ✨ title once generated.

## Generated titles & summaries

`--index` (or `t` on a row) samples user messages evenly across the **whole**
transcript (not just the first/last) plus the last assistant message, sends
that to `claude --model haiku -p` asking for the *main recurring topic*, and
caches the returned title in `~/.cache/clres/titles.json` (marked ✨ in the
list). `--summarize` (or `s` on a row) does the same for a 2-3 sentence
summary, shown in the status bar / a popup and included in search. Only real
conversations with ≥ `CLRES_MIN_ENTRIES` (15) transcript entries are
auto-indexed; `--reindex` also re-runs it on conversations that already have a
✨ title/summary (use after a title-generation change like this one). The
titler's own headless sessions are corralled into a throwaway
`/tmp/clres-titler` project and deleted, so they never pollute the list or
`claude --resume`.

Tunables (env): `CLRES_MIN_TITLE` (20 chars — hide shorter titles),
`CLRES_MIN_ENTRIES` (15), `CLRES_MODEL` (haiku).

## Projects

Each conversation is tagged with a **research-project slug** (mindsentences,
distraction, lppreadlisten, fusion, sevenT, syntax, config, obsidian,
personal, else misc) inferred from its cwd → branch → title via the ordered
`PROJECT_RULES` table. Classified rows show the project emoji + a coloured
project label; `p` cycles a project filter (header shows the active one).
Search (`/`) still matches the raw cwd basename too.

`PROJECTS` / `PROJECT_RULES` / `classify_project` are **deliberately duplicated**
between `clres.py` and the companion `ccview` tool's `ccview.py`, rather than
shared: ccview pipes its own source over ssh to run on remotes, so it has to
stay one self-contained file. The two copies must be kept in sync — edit one,
edit the other in the same commit. The only intended difference is the 3rd
`PROJECTS` field (a curses colour-pair index in clres, a `Colors` attribute
name in ccview).

## How it works

Reads `~/.claude/projects/*/*.jsonl` transcripts (honors
`CLAUDE_CONFIG_DIR`), takes the first real user prompt as the title
(slash commands are unwrapped, hook/system noise skipped), sorts by file
mtime. Subagent transcripts are picked up from the nested
`*/*/subagents/*.jsonl` layer. No dependencies beyond Python 3.10+ stdlib.

## License

MIT — see [LICENSE](LICENSE).
