# clres

Browse and resume your Claude Code conversations from a tiny terminal picker.

Each conversation gets an emoji (keyword-based, stable), its first prompt as
title, the project it belongs to, and its age. Hit Enter to resume it with
`claude --resume` from its original working directory.

```
 clres · 36/36 conversations          ↑↓ move · Enter resume · / filter · q quit
 🖥️ I'd like to make a little interface to browse...   claude            2m
 🐛 debugging AudioVolume decoding anomaly...          b1_mindsentences  3d
 📚 sphinx docs landing page refinement...             neuralhub-repo    5d
 ...
```

## Install (shell alias)

```bash
git clone <this repo> ~/git/clres
~/git/clres/install.sh   # adds `alias clres=...` to ~/.zshrc
```

## Install (Claude Code plugin)

Adds a `/clres` slash command that lists conversations inside a session:

```bash
claude plugin marketplace add ~/git/clres
claude plugin install clres@clres
```

## Usage

| invocation      | behavior                                    |
|-----------------|---------------------------------------------|
| `clres`         | interactive curses picker                   |
| `clres --list`  | plain table (also used when piped)          |
| `clres --json`  | machine-readable dump                       |
| `/clres`        | list inside a Claude Code session (plugin)  |

### Keys

- `↑/↓` or `j/k` — move · `g/G` — top/bottom · PgUp/PgDn
- type to filter (title + project), `Esc` clears
- `Enter` — resume session · `q` — quit

## How it works

Reads `~/.claude/projects/*/*.jsonl` transcripts (honors
`CLAUDE_CONFIG_DIR`), takes the first real user prompt as the title
(slash commands are unwrapped, hook/system noise skipped), sorts by file
mtime. No dependencies beyond Python 3.10+ stdlib.
