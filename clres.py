#!/usr/bin/env python3
"""clres — browse and resume Claude Code conversations.

Scans ~/.claude/projects/*/*.jsonl session transcripts, shows a small
curses picker (emoji + title + project + age), and resumes the selected
session with `claude --resume <id>` from its original working directory.

Usage:
  clres            interactive picker
  clres --list     plain table (no TTY needed)
  clres --json     machine-readable dump
"""

import curses
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"

# First keyword match wins; fallback is a hash-picked emoji so every
# conversation gets a stable icon.
EMOJI_KEYWORDS = [
    (r"\b(bug|fix|error|crash|broken|fail)", "🐛"),
    (r"\b(doc|readme|sphinx|docstring)", "📚"),
    (r"\b(plot|graph|figure|viz|visuali|chart|dashboard)", "📊"),
    (r"\b(test|pytest|ci\b)", "🧪"),
    (r"\b(git|commit|branch|merge|rebase|pr\b)", "🌿"),
    (r"\b(meg|eeg|fmri|brain|neuro|decod)", "🧠"),
    (r"\b(data|dataset|cache|download)", "📦"),
    (r"\b(refactor|clean|rename|reorganiz)", "🧹"),
    (r"\b(install|setup|config|env|venv|alias)", "🔧"),
    (r"\b(paper|article|cite|zotero|obsidian)", "📝"),
    (r"\b(gpu|cuda|torch|train|model)", "⚡"),
    (r"\b(ssh|cluster|server|remote|deploy)", "🛰️"),
    (r"\b(plugin|skill|hook|slash)", "🔌"),
    (r"\b(audio|sound|music|song|speech)", "🎵"),
    (r"\b(web|html|css|interface|ui\b|tui\b)", "🖥️"),
]
EMOJI_POOL = ["✨", "🌊", "🔮", "🌱", "🪐", "🍄", "🦎", "🌋", "🧭", "🎈", "🪶", "🌀"]


@dataclass
class Session:
    session_id: str
    title: str
    emoji: str
    cwd: str
    project: str
    mtime: float
    n_lines: int
    path: str


def _extract_title(entry: dict) -> str | None:
    """Pull a human title out of a user entry, or None if it's noise."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        content = " ".join(texts)
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    # Slash-command invocations arrive wrapped in XML-ish tags.
    m = re.search(r"<command-name>(.*?)</command-name>", text)
    if m:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        title = m.group(1).strip()
        if args and args.group(1).strip():
            title += " " + args.group(1).strip()
        return title
    if text.startswith("<"):  # system-reminder, hook payloads, etc.
        return None
    if text.startswith("Caveat: the messages below"):
        return None
    return re.sub(r"\s+", " ", text)


def _pick_emoji(title: str, session_id: str) -> str:
    low = title.lower()
    for pattern, emoji in EMOJI_KEYWORDS:
        if re.search(pattern, low):
            return emoji
    return EMOJI_POOL[int(session_id.replace("-", "")[:8], 16) % len(EMOJI_POOL)]


def scan_sessions() -> list[Session]:
    sessions = []
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        title, cwd = None, None
        n_lines = 0
        try:
            with open(jsonl, errors="replace") as fh:
                for line in fh:
                    n_lines += 1
                    if title is not None and cwd is not None:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cwd is None and entry.get("cwd"):
                        cwd = entry["cwd"]
                    if title is None:
                        title = _extract_title(entry)
        except OSError:
            continue
        if title is None:
            continue  # no real user prompt -> not worth listing
        cwd = cwd or str(Path.home())
        sid = jsonl.stem
        sessions.append(Session(
            session_id=sid,
            title=title,
            emoji=_pick_emoji(title, sid),
            cwd=cwd,
            project=Path(cwd).name or cwd,
            mtime=jsonl.stat().st_mtime,
            n_lines=n_lines,
            path=str(jsonl),
        ))
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def rel_age(ts: float) -> str:
    delta = time.time() - ts
    for unit, sec in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= sec:
            return f"{int(delta // sec)}{unit}"
    return "now"


def resume(session: Session) -> None:
    cwd = session.cwd if os.path.isdir(session.cwd) else str(Path.home())
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--resume", session.session_id])


# ---------------------------------------------------------------- TUI

def run_tui(stdscr, sessions: list[Session]):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # project
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # age
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # header/filter

    selected, offset, query = 0, 0, ""

    def filtered():
        if not query:
            return sessions
        q = query.lower()
        return [s for s in sessions if q in s.title.lower() or q in s.project.lower()]

    while True:
        rows = filtered()
        selected = max(0, min(selected, len(rows) - 1))
        h, w = stdscr.getmaxyx()
        list_h = h - 3
        if selected < offset:
            offset = selected
        if selected >= offset + list_h:
            offset = selected - list_h + 1
        offset = max(0, offset)

        stdscr.erase()
        header = f" clres · {len(rows)}/{len(sessions)} conversations "
        hint = " ↑↓ move · Enter resume · / filter · q quit "
        stdscr.addnstr(0, 0, header, w - 1, curses.color_pair(4) | curses.A_BOLD)
        stdscr.addnstr(0, max(0, w - len(hint) - 1), hint, w - 1, curses.A_DIM)

        for i, s in enumerate(rows[offset:offset + list_h]):
            y = i + 1
            is_sel = (offset + i) == selected
            age = rel_age(s.mtime).rjust(4)
            proj = s.project[:16].ljust(16)
            title_w = max(10, w - 30)
            title = s.title[:title_w]
            if is_sel:
                stdscr.addnstr(y, 0, " " * (w - 1), w - 1, curses.color_pair(3))
                stdscr.addnstr(y, 1, f"{s.emoji} {title}", w - 26, curses.color_pair(3) | curses.A_BOLD)
                stdscr.addnstr(y, max(0, w - 23), f"{proj} {age} ", 22, curses.color_pair(3))
            else:
                stdscr.addnstr(y, 1, f"{s.emoji} {title}", w - 26)
                stdscr.addnstr(y, max(0, w - 23), proj, 17, curses.color_pair(1))
                stdscr.addnstr(y, max(0, w - 6), age, 5, curses.color_pair(2))

        if rows and 0 <= selected < len(rows):
            s = rows[selected]
            status = f" {s.session_id[:8]} · {s.cwd} · {s.n_lines} entries "
        else:
            status = " no match "
        stdscr.addnstr(h - 2, 0, status[:w - 1], w - 1, curses.A_DIM)
        prompt = f" /{query}" if query else ""
        stdscr.addnstr(h - 1, 0, prompt[:w - 1], w - 1, curses.color_pair(4))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27) and not query:
            return None
        elif key == 27:
            query = ""
        elif key in (curses.KEY_DOWN, ord("j")):
            selected += 1
        elif key in (curses.KEY_UP, ord("k")):
            selected -= 1
        elif key == ord("g"):
            selected = 0
        elif key == ord("G"):
            selected = len(rows) - 1
        elif key in (curses.KEY_NPAGE,):
            selected += list_h
        elif key in (curses.KEY_PPAGE,):
            selected -= list_h
        elif key in (curses.KEY_ENTER, 10, 13):
            if rows:
                return rows[selected]
        elif key == ord("/"):
            query = ""
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
        elif 32 <= key < 127:
            query += chr(key)
            selected = 0


def print_list(sessions: list[Session]) -> None:
    for s in sessions:
        print(f"{s.emoji}  {rel_age(s.mtime):>4}  {s.project[:18]:<18}  {s.title[:80]}")


def main() -> None:
    sessions = scan_sessions()
    if not sessions:
        print("No Claude Code conversations found under", PROJECTS_DIR)
        sys.exit(1)
    if "--json" in sys.argv:
        print(json.dumps([s.__dict__ for s in sessions], indent=2))
        return
    if "--list" in sys.argv or not sys.stdout.isatty():
        print_list(sessions)
        return
    choice = curses.wrapper(run_tui, sessions)
    if choice is not None:
        print(f"{choice.emoji} resuming: {choice.title[:70]}  ({choice.session_id[:8]})")
        resume(choice)


if __name__ == "__main__":
    main()
