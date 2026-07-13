# clres

Browse and resume your Claude Code conversations from a tiny terminal picker.

Each conversation gets an emoji (keyword-based, stable), its first prompt as
title, the project it belongs to, and its age. Hit Enter to resume it with
`claude --resume` from its original working directory.

```
 clres · 19/36 conversations   ↑↓ move · Enter resume · t title · a all · q quit
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

| invocation      | behavior                                            |
|-----------------|-----------------------------------------------------|
| `clres`         | interactive curses picker                           |
| `clres --all`   | include tiny conversations (bare `/model`, "hi", …) |
| `clres --index` | haiku-title every large untitled conversation       |
| `clres --list`  | plain table (also used when piped)                  |
| `clres --json`  | machine-readable dump                               |
| `/clres`        | list inside a Claude Code session (plugin)          |

### Keys

- `↑/↓` or `j/k` — move · `g/G` — top/bottom · PgUp/PgDn
- type to filter (title + project), `Esc` clears
- `Enter` — resume session · `t` — haiku-title selected · `a` — toggle tiny · `q` — quit

## Generated titles

`--index` (or `t` on a row) sends the first user prompt + last assistant
message of a conversation to `claude --model haiku -p` and caches the
returned title in `~/.cache/clres/titles.json` (marked ✨ in the list).
Only conversations with ≥ `CLRES_MIN_ENTRIES` (50) transcript entries are
auto-indexed. The titler's own headless sessions are corralled into a
throwaway `/tmp/clres-titler` project and deleted, so they never pollute
the list or `claude --resume`.

Tunables (env): `CLRES_MIN_TITLE` (20 chars — hide shorter titles),
`CLRES_MIN_ENTRIES` (50), `CLRES_MODEL` (haiku).

## How it works

Reads `~/.claude/projects/*/*.jsonl` transcripts (honors
`CLAUDE_CONFIG_DIR`), takes the first real user prompt as the title
(slash commands are unwrapped, hook/system noise skipped), sorts by file
mtime. No dependencies beyond Python 3.10+ stdlib.
