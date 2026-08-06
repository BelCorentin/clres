#!/usr/bin/env python3
"""clres — browse and resume Claude Code conversations.

Scans ~/.claude/projects/*/*.jsonl session transcripts, shows a small
curses picker (emoji + title + project + age), and resumes the selected
session with `claude --resume <id>` from its original working directory.

Tiny conversations (title shorter than CLRES_MIN_TITLE chars, e.g. bare
`/model` calls) are hidden by default. Large conversations can get a
haiku-generated title, cached in ~/.cache/clres/titles.json.

Usage:
  clres              interactive picker
  clres -d 7         look back 7 days (--days; 0 = no time limit)
  clres --all        include tiny + headless + agent transcripts, no time limit
  clres --index      generate haiku titles for all untitled real convos
  clres --summarize  generate haiku summaries for all real convos
  clres --list       plain table (no TTY needed)
  clres --json       machine-readable dump
"""

import argparse
import curses
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
GOALS_DIR = CLAUDE_DIR / "goals"   # session-state.sh registry (<sid>.json)
COMEBACK_DIR = CLAUDE_DIR / "comeback"   # marker files: <sid> present == "might come back"
DEFAULT_DAYS = 1                   # focused view looks back this many days
DAY_LADDER = [1, 3, 7, 14, 30, 0]  # `[` / `]` step through these; 0 == no limit
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "clres" / "titles.json"

MIN_TITLE_CHARS = int(os.environ.get("CLRES_MIN_TITLE", "20"))  # hide shorter
MIN_ENTRIES = int(os.environ.get("CLRES_MIN_ENTRIES", "15"))  # --index eligibility
TITLE_MODEL = os.environ.get("CLRES_MODEL", "haiku")
# The titler's own headless `claude -p` calls get logged as sessions too;
# corral them into one throwaway project dir that clres skips and deletes.
TITLER_CWD = Path("/tmp/clres-titler")
TITLER_SLUG = "-tmp-clres-titler"

# First keyword match wins; fallback is a hash-picked emoji so every
# conversation gets a stable icon.
EMOJI_KEYWORDS = [
    (r"\b(bug|fix|error|crash|broken|fail|debug)", "🐛"),
    (r"\b(doc|readme|sphinx|docstring)", "📚"),
    (r"\b(plot|graph|figure|viz|visuali|chart|dashboard)", "📊"),
    (r"\b(test|pytest|ci\b)", "🧪"),
    (r"\b(plugin|skill|hook|slash)", "🔌"),
    (r"\b(git|commit|branch|merge|rebase|pr\b)", "🌿"),
    (r"\b(meg|eeg|fmri|brain|neuro|decod)", "🧠"),
    (r"\b(data|dataset|cache|download)", "📦"),
    (r"\b(refactor|clean|rename|reorganiz)", "🧹"),
    (r"\b(install|setup|config|env|venv|alias)", "🔧"),
    (r"\b(paper|article|cite|zotero|obsidian)", "📝"),
    (r"\b(gpu|cuda|torch|train|model)", "⚡"),
    (r"\b(ssh|cluster|server|remote|deploy)", "🛰️"),
    (r"\b(audio|sound|music|song|speech)", "🎵"),
    (r"\b(web|html|css|interface|ui\b|tui\b|browser|widget)", "🖥️"),
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
    size: int
    generated: bool
    headless: bool
    summary: str
    path: str
    branch: str = ""      # git branch (+ ' @worktree' if linked)
    comeback: bool = False   # flagged "might come back" (~/.claude/comeback/<sid>)
    pslug: str = "misc"   # research-project slug (classify_project)
    agent: bool = False   # subagent transcript (<sid>/subagents/agent-*.jsonl)
    resume_id: str = ""   # session to `claude --resume` (parent, for agents)

    @property
    def small(self) -> bool:
        """Hidden by default: headless agent sessions (statusline bots,
        sdk calls), subagent transcripts, and conversations too tiny to
        have a real title."""
        return (self.headless or self.agent
                or (not self.generated and len(self.title) < MIN_TITLE_CHARS))


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=1))


def _user_text(entry: dict, allow_sidechain: bool = False) -> str | None:
    """Pull a human title out of a user entry, or None if it's noise.

    Sidechain entries are skipped by default (they're subagent turns bleeding
    into a main transcript); *allow_sidechain* is used when reading a subagent
    transcript, where every entry is a sidechain by construction."""
    if entry.get("type") != "user":
        return None
    if entry.get("isSidechain") and not allow_sidechain:
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


def is_comeback(sid: str) -> bool:
    return (COMEBACK_DIR / sid).exists()


def toggle_comeback(sid: str) -> bool:
    """Flip the 'might come back' flag for *sid*. Returns the new state."""
    COMEBACK_DIR.mkdir(parents=True, exist_ok=True)
    marker = COMEBACK_DIR / sid
    if marker.exists():
        marker.unlink()
        return False
    marker.touch()
    return True


_GIT_CACHE: dict[str, str] = {}


def _git_brief(cwd: str) -> str:
    """Current branch for *cwd* (+ ' @<name>' in a linked worktree). Cached per cwd."""
    if not cwd:
        return ""
    if cwd in _GIT_CACHE:
        return _GIT_CACHE[cwd]
    br = ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            br = r.stdout.strip()
            if br == "HEAD":
                s = subprocess.run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True, timeout=2)
                br = s.stdout.strip() or "detached"
            gd = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"],
                                capture_output=True, text=True, timeout=2).stdout.strip()
            if "/worktrees/" in gd:
                br += " @" + os.path.basename(gd)
    except (OSError, subprocess.SubprocessError):
        br = ""
    _GIT_CACHE[cwd] = br
    return br


def _registry_state(sid: str) -> dict:
    """Live session state written by the session-state.sh hook, or {}.
    Shared source with the statusline, ntfy pushes, and ccview."""
    try:
        d = json.loads((GOALS_DIR / f"{sid}.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pick_emoji(title: str, session_id: str) -> str:
    low = title.lower()
    for pattern, emoji in EMOJI_KEYWORDS:
        if re.search(pattern, low):
            return emoji
    try:                      # stable per-session fallback icon
        n = int(session_id.replace("-", "")[:8], 16)
    except ValueError:        # non-hex ids (e.g. agent-<hash>)
        n = sum(session_id.encode())
    return EMOJI_POOL[n % len(EMOJI_POOL)]


# ---------------------------------------------------------------- projects
# Tag every conversation with a research-project slug from its cwd / branch /
# title so the picker can icon, colour and filter by project. Ordered rules,
# first match wins; fields per rule: b=branch, c=cwd, t=title+name.
#
# !! KEEP IN SYNC WITH ccview.py (~/git/ccview/ccview.py) !!
# PROJECTS / PROJECT_RULES / classify_project are DUPLICATED on purpose, not
# shared via an import or a config file: ccview pipes its own source over ssh
# to run on remotes, so it must stay a single self-contained file. Change one
# table, change the other in the same commit. The only intended difference is
# the 3rd PROJECTS field — a curses colour-pair index here, a Colors attribute
# name in ccview.
PROJECTS = {                       # slug -> (emoji, label, color-pair index)
    "mindsentences": ("🧠", "MindSentences",   5),
    "distraction":   ("🎯", "Distraction",     2),
    "lppreadlisten": ("📖", "LPP Read-Listen", 1),
    "fusion":        ("🔗", "fMEGRI Fusion",   6),
    "sevenT":        ("🎏", "7T",              7),
    "syntax":        ("🌳", "Syntax",          7),
    "config":        ("🔧", "claude-config",   1),
    "obsidian":      ("📝", "Obsidian",        6),
    "personal":      ("🎈", "Personal",        8),
    "misc":          ("•",  "misc",            0),
}
PROJECT_ORDER = list(PROJECTS)

PROJECT_RULES = [                  # (fields, regex, slug)
    ("bct", r"bonnaire",                                   "distraction"),
    ("bc", r"mentalizing_ext|mentalizing",                "mindsentences"),
    ("bct", r"lppreadlisten|read.?listen|petit(read|listen)", "lppreadlisten"),
    ("bt", r"\bdistraction\b",                             "distraction"),
    ("b",  r"\bsyntax\b",                                   "syntax"),
    ("ct", r"fmegri|megri|\bfusi\b|\bfusion\b",            "fusion"),
    ("t",  r"\b7[\s-]?t(esla)?\b",                          "sevenT"),
    ("ct", r"\.claude|ccview|clres|caveman|claude-config", "config"),
    ("ct", r"obsidian|\bvault\b",                           "obsidian"),
    ("ct", r"petite.?sauvag|\bfestival\b|sauvage",         "personal"),
    ("ct", r"mindsent|mentalizing|hierarchy paper",        "mindsentences"),
    ("t",  r"\bsyntax\b",                                   "syntax"),
    ("c",  r"/brainai(/|$)",                                "mindsentences"),
]

_PROJ_CACHE: dict[tuple, str] = {}


def classify_project(cwd: str = "", branch: str = "", title: str = "",
                     name: str = "") -> str:
    key = (cwd, branch, title, name)
    hit = _PROJ_CACHE.get(key)
    if hit is not None:
        return hit
    hay = {"b": (branch or "").lower(), "c": (cwd or "").lower(),
           "t": f"{title or ''} {name or ''}".lower()}
    slug = "misc"
    for fields, pat, cand in PROJECT_RULES:
        if any(re.search(pat, hay[f]) for f in fields):
            slug = cand
            break
    _PROJ_CACHE[key] = slug
    return slug


def _transcripts() -> list[tuple[Path, bool]]:
    """(path, is_agent) for every transcript under PROJECTS_DIR.

    Top-level `<project>/<sid>.jsonl` are real sessions; the nested
    `<project>/<sid>/subagents/agent-*.jsonl` are subagent (Task tool)
    transcripts, listed as children and hidden unless --all."""
    found: list[tuple[Path, bool]] = []
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        if jsonl.parent.name != TITLER_SLUG:
            found.append((jsonl, False))
    for jsonl in PROJECTS_DIR.glob("*/*/subagents/*.jsonl"):
        # <project>/<sid>/subagents/agent-x.jsonl -> parents[2] is the project
        if jsonl.parents[2].name != TITLER_SLUG:
            found.append((jsonl, True))
    return found


def scan_sessions(cache: dict) -> list[Session]:
    sessions = []
    for jsonl, is_agent in _transcripts():
        title, cwd, entrypoint = None, None, None
        parent_sid = ""
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
                    if is_agent and not parent_sid and entry.get("sessionId"):
                        parent_sid = entry["sessionId"]
                    if title is None:
                        title = _user_text(entry, allow_sidechain=is_agent)
                        if title is not None:
                            entrypoint = entry.get("entrypoint", "cli")
            stat = jsonl.stat()
        except OSError:
            continue
        if title is None:
            continue  # no real user prompt -> not worth listing
        sid = jsonl.stem
        if is_agent:
            # Resuming an agent transcript is meaningless — point at the
            # parent session (the dir name is the sid when the entry lacks it).
            parent_sid = parent_sid or jsonl.parent.parent.name
        generated = False
        emoji = None
        summary = cache.get(sid, {}).get("summary", "")
        cached = cache.get(sid, {}).get("title")
        if cached:
            title, generated = cached, True
        else:
            # Prefer the live session-state registry goal over the raw first
            # prompt — it's summarized at turn end and shared with the
            # statusline/ntfy/ccview, so no extra Haiku call here.
            reg = _registry_state(sid)
            if reg.get("goal"):
                title, generated = reg["goal"], True
                emoji = reg.get("emoji") or None
                if not summary and reg.get("detail"):
                    summary = reg["detail"]
        if is_agent:
            # After the cache/registry override, so a haiku-titled 🤖 row
            # keeps its prefix instead of losing it.
            title = f"agent: {title}"
        cwd = cwd or str(Path.home())
        sessions.append(Session(
            session_id=sid,
            title=title,
            emoji="🤖" if is_agent else (emoji or _pick_emoji(title, sid)),
            cwd=cwd,
            project=Path(cwd).name or cwd,
            mtime=stat.st_mtime,
            n_lines=n_lines,
            size=stat.st_size,
            generated=generated,
            headless=entrypoint not in ("cli", "claude-desktop"),
            summary=summary,
            path=str(jsonl),
            branch=_git_brief(cwd),
            comeback=is_comeback(sid),
            pslug=classify_project(cwd, _git_brief(cwd), title),
            agent=is_agent,
            resume_id=parent_sid,
        ))
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


# ------------------------------------------------------- title generation

def _endpoints(path: str) -> tuple[str, str]:
    """First user prompt + last assistant text of a transcript."""
    first, last_raw = "", None
    with open(path, errors="replace") as fh:
        for line in fh:
            if not first and '"type":"user"' in line[:400]:
                try:
                    t = _user_text(json.loads(line))
                    if t:
                        first = t
                except json.JSONDecodeError:
                    pass
            if '"type":"assistant"' in line and '"isSidechain":true' not in line:
                last_raw = line
    last = ""
    if last_raw:
        try:
            content = json.loads(last_raw).get("message", {}).get("content", [])
            if isinstance(content, list):
                last = " ".join(c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text")
        except json.JSONDecodeError:
            pass
    return first[:1500], re.sub(r"\s+", " ", last).strip()[:1500]


def _ask_haiku(prompt: str) -> str | None:
    try:
        TITLER_CWD.mkdir(exist_ok=True)
        out = subprocess.run(
            ["claude", "--model", TITLE_MODEL, "-p", prompt],
            capture_output=True, text=True, timeout=120, cwd=TITLER_CWD,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        shutil.rmtree(PROJECTS_DIR / TITLER_SLUG, ignore_errors=True)
    return out.stdout.strip() or None


def generate_title(session: Session) -> str | None:
    first, last = _endpoints(session.path)
    if not first and not last:
        return None
    out = _ask_haiku(
        "Write a short descriptive title (max 8 words, no quotes, no trailing "
        "period) for this coding-assistant conversation, based on its first "
        "user message and last assistant message. Output the title only.\n\n"
        f"FIRST USER MESSAGE:\n{first}\n\nLAST ASSISTANT MESSAGE:\n{last}"
    )
    return out.splitlines()[0].strip(' "\'')[:100] if out else None


def generate_summary(session: Session) -> str | None:
    first, last = _endpoints(session.path)
    if not first and not last:
        return None
    out = _ask_haiku(
        "Summarize this coding-assistant conversation in 2-3 plain sentences: "
        "what the user wanted and where it ended up. No preamble.\n\n"
        f"FIRST USER MESSAGE:\n{first}\n\nLAST ASSISTANT MESSAGE:\n{last}"
    )
    return re.sub(r"\s+", " ", out).strip()[:600] if out else None


def _cache_set(cache: dict, sid: str, **fields) -> None:
    entry = cache.setdefault(sid, {})
    entry.update(fields, generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    save_cache(cache)


def apply_title(session: Session, title: str, cache: dict) -> None:
    session.title = title
    session.generated = True
    session.emoji = _pick_emoji(title, session.session_id)
    _cache_set(cache, session.session_id, title=title)


def apply_summary(session: Session, summary: str, cache: dict) -> None:
    session.summary = summary
    _cache_set(cache, session.session_id, summary=summary)


def index_titles(sessions: list[Session], cache: dict) -> None:
    todo = [s for s in sessions
            if not s.generated and not s.headless and not s.agent
            and s.n_lines >= MIN_ENTRIES]
    if not todo:
        print("All conversations already titled.")
        return
    print(f"Titling {len(todo)} conversations with {TITLE_MODEL}...")
    for i, s in enumerate(todo, 1):
        title = generate_title(s)
        if title:
            apply_title(s, title, cache)
            print(f"  [{i}/{len(todo)}] {s.emoji} {title}")
        else:
            print(f"  [{i}/{len(todo)}] failed: {s.session_id[:8]}")


def index_summaries(sessions: list[Session], cache: dict) -> None:
    todo = [s for s in sessions
            if not s.summary and not s.headless and not s.agent
            and s.n_lines >= MIN_ENTRIES]
    if not todo:
        print("All conversations already summarized.")
        return
    print(f"Summarizing {len(todo)} conversations with {TITLE_MODEL}...")
    for i, s in enumerate(todo, 1):
        summary = generate_summary(s)
        if summary:
            apply_summary(s, summary, cache)
            print(f"  [{i}/{len(todo)}] {s.emoji} {s.title[:40]}: {summary[:70]}")
        else:
            print(f"  [{i}/{len(todo)}] failed: {s.session_id[:8]}")


# ---------------------------------------------------------------- misc

def rel_age(ts: float) -> str:
    delta = time.time() - ts
    for unit, sec in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= sec:
            return f"{int(delta // sec)}{unit}"
    return "now"


def window_label(days: int) -> str:
    """Header text for a lookback window (0 == no limit)."""
    return "all" if not days else f"last {days}d"


def in_window(mtime: float, days: int, now: float | None = None) -> bool:
    """True if *mtime* falls inside a *days*-day lookback (0 == no limit)."""
    if not days:
        return True
    return ((now if now is not None else time.time()) - mtime) < days * 86400


def resume(session: Session) -> None:
    cwd = session.cwd if os.path.isdir(session.cwd) else str(Path.home())
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--resume", session.resume_id or session.session_id])


# ---------------------------------------------------------------- TUI

MIN_POPUP_H = 5    # box border + title row + >=1 body row + footer border
MIN_POPUP_W = 14   # border + 2 padding cols on each side + >=1 text col
MIN_TUI_W = 30     # title col + the 23-col project/age gutter + breathing room
MIN_TUI_H = 5      # header + >=1 list row + status + search line


KEY_POLL_MS = 500  # see _read_wch


def _arm_key_timeout(win):
    """Make `win` give up on an incomplete key sequence instead of wedging.

    With no timeout, ncurses' get_wch() blocks *forever* on a byte that cannot
    start a valid UTF-8 sequence (a latin-1 paste, or any keypress at all under
    LC_ALL=C): it sits waiting for continuation bytes that never come, and every
    later keypress is swallowed too — the TUI is dead and needs SIGKILL. A
    timeout makes ncurses abandon the partial sequence and return ERR, so the
    reader below can drop it and carry on."""
    try:
        win.timeout(KEY_POLL_MS)
    except curses.error:
        pass


def _read_wch(win):
    """Read ONE whole keypress from `win` — the wide-aware getch().

    Must consume the entire multi-byte sequence. A bare win.getch() eats only
    the first UTF-8 byte of 'é'/'漢' and leaves the continuation bytes sitting
    in the tty, which then makes the next read choke on an invalid sequence.

    Blocks until a key actually arrives, but only in KEY_POLL_MS slices, so an
    undecodable byte costs one dropped keypress rather than the session.
    Returns a str for a character, an int for a KEY_* constant."""
    _arm_key_timeout(win)
    while True:
        try:
            k = win.get_wch()
        except curses.error:
            continue          # poll tick, or a partial sequence just abandoned
        except ValueError:
            continue          # undecodable byte — drop it, stay in wide mode
        except AttributeError:
            break             # no wide input on this curses build
        if k != -1:
            return k
    while True:
        try:
            k = win.getch()
        except curses.error:
            continue
        if k != -1:
            return k


def _popup(stdscr, title: str, text: str) -> bool:
    """Draw a centred modal box. Returns False (drawing nothing) when the
    terminal is too small to hold a legible box — callers flash instead."""
    h, w = stdscr.getmaxyx()
    import textwrap
    if h < MIN_POPUP_H or w < MIN_POPUP_W:
        return False
    box_w = max(MIN_POPUP_W, min(w - 4, 90))
    box_w = min(box_w, w)
    wrap_w = max(1, box_w - 4)             # textwrap rejects width <= 0
    lines = []
    for para in text.split("\n"):          # keep explicit line breaks
        lines.extend(textwrap.wrap(para, wrap_w) or [""])
    lines = lines or ["(empty)"]
    box_h = max(MIN_POPUP_H, min(len(lines) + 4, h - 2))
    box_h = min(box_h, h)
    body_h = max(0, box_h - 4)             # never slice negative
    y0, x0 = max(0, (h - box_h) // 2), max(0, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.erase()
    win.box()
    win.addnstr(0, 2, f" {title} ", wrap_w, curses.A_BOLD)
    for i, line in enumerate(lines[:body_h]):
        win.addnstr(i + 2, 2, line, wrap_w)
    win.addnstr(box_h - 1, 2, " any key to close ", wrap_w, curses.A_DIM)
    win.refresh()
    _read_wch(win)          # NOT getch(): must swallow the whole keypress
    return True


def run_tui(stdscr, sessions: list[Session], cache: dict, show_all: bool,
            days: int = DEFAULT_DAYS):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # project
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # age
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # header/filter
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # project colours (5-8)
    curses.init_pair(6, curses.COLOR_BLUE, -1)
    curses.init_pair(7, curses.COLOR_GREEN, -1)
    curses.init_pair(8, curses.COLOR_RED, -1)

    selected, offset, query = 0, 0, ""
    search_mode = False
    focus = True                 # default view: flagged "might come back" + recent
    project_filter = None        # active project slug filter (cycled with p)
    flash = ""
    # Lookback ladder, with the CLI-supplied window spliced in if it's custom.
    ladder = sorted(set(DAY_LADDER) | ({days} if days > 0 else set()),
                    key=lambda d: (d == 0, d))
    window = days if days in ladder else DEFAULT_DAYS
    use_wch = hasattr(stdscr, "get_wch")
    window_note = ""             # set by filtered() when the window came up empty

    def filtered():
        nonlocal window_note
        window_note = ""
        rows = sessions if show_all else [s for s in sessions if not s.small]
        if not rows:
            rows = sessions
        if focus and window:
            now = time.time()
            foc = [s for s in rows if s.comeback or in_window(s.mtime, window, now)]
            if foc:
                rows = foc
            else:
                # Never render an empty screen: keep the wider list and say why.
                window_note = f"no sessions in the {window_label(window)} — showing all"
        if project_filter:
            rows = [s for s in rows if s.pslug == project_filter]
        if query:
            q = query.lower()
            rows = [s for s in rows if q in s.title.lower() or q in s.project.lower()
                    or q in s.summary.lower()]
        return rows

    def read_key():
        """Read one keypress as ("ch", str) or ("key", int).

        The two spaces overlap numerically — curses.KEY_ENTER == 343 ==
        ord('ŗ'), KEY_BACKSPACE == 263 == ord('ć') — so the kind tag, not the
        value, is what tells "the user typed ŗ" from "the user hit Enter".
        Never compare a typed character against a curses KEY_* constant.

        get_wch() is what makes accented/unicode chars typable in the search
        box; getch() is the fallback if the terminal or curses build can't do
        wide input (there, >= 256 is a KEY_* constant by construction)."""
        nonlocal use_wch
        if use_wch:
            # Timeout-sliced so an undecodable byte (latin-1 paste, LC_ALL=C)
            # can't wedge ncurses forever — see _arm_key_timeout.
            _arm_key_timeout(stdscr)
            while True:
                try:
                    k = stdscr.get_wch()
                except curses.error:
                    continue      # poll tick / abandoned partial sequence
                except ValueError:
                    continue      # undecodable byte: drop it, stay wide
                except AttributeError:
                    use_wch = False   # this build really has no wide input
                    break
                if k == -1:
                    continue
                return ("ch", k) if isinstance(k, str) else ("key", k)
        while True:
            try:
                k = stdscr.getch()
            except curses.error:
                continue
            if k != -1:       # -1 is the timeout tick, not a keypress
                break
        if 32 <= k < 127 or k in (8, 9, 10, 13, 27, 127):
            return "ch", chr(k)
        return "key", k

    while True:
        rows = filtered()
        selected = max(0, min(selected, len(rows) - 1))
        h, w = stdscr.getmaxyx()
        if w < MIN_TUI_W or h < MIN_TUI_H:
            # Below this the rows can't hold title + project + age and ncurses
            # starts returning ERR mid-write, which kills the TUI with a
            # traceback before any key is even read. Say so instead.
            stdscr.erase()
            for i, line in enumerate(("terminal too narrow",
                                      f"{w}x{h} < {MIN_TUI_W}x{MIN_TUI_H}",
                                      "resize, or q to quit")):
                if i < h:
                    stdscr.addnstr(i, 0, line, max(1, w - 1))
            stdscr.refresh()
            kind, val = read_key()
            if kind == "ch" and val in ("q", "\x1b"):
                return None
            continue         # anything else: re-measure (handles KEY_RESIZE)
        list_h = h - 3
        if selected < offset:
            offset = selected
        if selected >= offset + list_h:
            offset = selected - list_h + 1
        offset = max(0, offset)

        stdscr.erase()
        scope = " (all)" if show_all else (" 🔖focus" if focus else " (recent+old)")
        win = f" · {window_label(window) if focus else 'all'}"
        pf = f" · {PROJECTS[project_filter][0]} {PROJECTS[project_filter][1]}" if project_filter else ""
        header = f" clres · {len(rows)}/{len(sessions)}{scope}{win}{pf} "
        hint = (" type to search · Enter done · Esc cancel " if search_mode else
                " ↑↓ · ⏎ resume · [ ] days · p proj · m 🔖 · c focus · / search · s sum · a all · ? · q ")
        stdscr.addnstr(0, 0, header, w - 1, curses.color_pair(4) | curses.A_BOLD)
        stdscr.addnstr(0, max(0, w - len(hint) - 1), hint, w - 1, curses.A_DIM)

        for i, s in enumerate(rows[offset:offset + list_h]):
            y = i + 1
            is_sel = (offset + i) == selected
            age = rel_age(s.mtime).rjust(4)
            pemoji, plabel, pcolor = PROJECTS[s.pslug]
            icon = "🤖" if s.agent else (pemoji if s.pslug != "misc" else s.emoji)
            proj = (plabel if s.pslug != "misc" else s.project)[:16].ljust(16)
            flag = "🔖" if s.comeback else "  "
            title = s.title
            if is_sel:
                stdscr.addnstr(y, 0, " " * (w - 1), w - 1, curses.color_pair(3))
                stdscr.addnstr(y, 1, f"{flag}{icon} {title}", max(1, w - 26), curses.color_pair(3) | curses.A_BOLD)
                stdscr.addnstr(y, max(0, w - 23), f"{proj} {age} ", 22, curses.color_pair(3))
            else:
                attr = curses.A_DIM if s.small else 0
                stdscr.addnstr(y, 1, f"{flag}{icon} {title}", max(1, w - 26), attr)
                stdscr.addnstr(y, max(0, w - 23), proj, 17, curses.color_pair(pcolor))
                stdscr.addnstr(y, max(0, w - 6), age, 5, curses.color_pair(2))

        if flash:
            status = f" {flash} "
        elif window_note:
            status = f" {window_note} "
        elif rows and 0 <= selected < len(rows):
            s = rows[selected]
            if s.summary:
                status = f" {s.summary} "
            else:
                gen = " · ✨titled" if s.generated else ""
                br = f" · ⎇ {s.branch}" if s.branch else ""
                status = f" {s.session_id[:8]} · {s.cwd}{br} · {s.n_lines} entries · {s.size // 1024}K{gen} "
        else:
            status = " no match "
        stdscr.addnstr(h - 2, 0, status[:w - 1], w - 1, curses.A_DIM)
        if search_mode or query:
            cursor = "█" if search_mode else ""
            stdscr.addnstr(h - 1, 0, f" /{query}{cursor}"[:w - 1], w - 1, curses.color_pair(4))
        stdscr.refresh()
        flash = ""

        def busy(msg):
            stdscr.addnstr(h - 2, 0, f" ✨ {msg}... "[:w - 1], w - 1, curses.color_pair(4))
            stdscr.refresh()

        kind, val = read_key()
        # Exactly one of these is ever set: `ch` for a character the user
        # typed, `key` for a curses KEY_* constant. Dispatch on the right one.
        ch = val if kind == "ch" else None
        key = val if kind == "key" else None
        if search_mode:
            if ch in ("\n", "\r") or key == curses.KEY_ENTER:
                search_mode = False
            elif ch == "\x1b":  # Esc: cancel search
                search_mode, query = False, ""
            elif ch in ("\x7f", "\x08") or key == curses.KEY_BACKSPACE:
                query = query[:-1]
            elif key == curses.KEY_DOWN:
                selected += 1
            elif key == curses.KEY_UP:
                selected -= 1
            elif ch and ch.isprintable():   # any codepoint, incl. é à ç ŗ 漢
                query += ch
                selected = 0
            continue
        if ch == "q":
            return None
        elif ch == "\x1b":
            if query:
                query = ""
            else:
                return None
        elif ch == "/":
            search_mode = True
        elif ch == "j" or key == curses.KEY_DOWN:
            selected += 1
        elif ch == "k" or key == curses.KEY_UP:
            selected -= 1
        elif ch == "m":
            if rows:
                s = rows[selected]
                s.comeback = toggle_comeback(s.session_id)
                flash = f"🔖 flagged: {s.title[:50]}" if s.comeback else f"unflagged: {s.title[:50]}"
        elif ch == "c":
            focus = not focus
            selected = 0
            flash = "🔖 focus: might-come-back + recent" if focus else "showing recent + old"
        elif ch in ("[", "-", "]", "+", "="):
            step = -1 if ch in ("[", "-") else 1
            i = ladder.index(window) if window in ladder else 0
            window = ladder[max(0, min(len(ladder) - 1, i + step))]
            focus = True         # the window only bites in the focused view
            selected = 0
            flash = f"window: {window_label(window)}"
        elif ch == "?":
            if not _popup(stdscr, "clres keys",
                          "↑↓ / j k   move          g G   top/bottom      PgUp/PgDn  page\n"
                          "⏎          resume        q     quit            Esc  clear/quit\n"
                          "[ ]  (or - +)  shrink / grow the lookback window\n"
                          f"             ladder: {' · '.join(window_label(d) for d in ladder)}\n"
                          "c          focused view (🔖-flagged + window) on/off\n"
                          "p          cycle project filter    m   flag/unflag 🔖\n"
                          "/          search title+project+summary (unicode ok)\n"
                          "t          re-title      s   summary popup\n"
                          "a          show hidden rows (tiny / headless / 🤖 subagent)"):
                flash = "terminal too small for the help box"
        elif ch == "p":
            present = [p for p in PROJECT_ORDER
                       if any(s.pslug == p for s in (sessions if show_all
                                                     else [x for x in sessions if not x.small]))]
            cyc = [None] + present
            try:
                i = cyc.index(project_filter)
            except ValueError:
                i = 0
            project_filter = cyc[(i + 1) % len(cyc)]
            selected = 0
            flash = (f"project: {PROJECTS[project_filter][1]}" if project_filter
                     else "project: all")
        elif ch == "g":
            selected = 0
        elif ch == "G":
            selected = len(rows) - 1
        elif key == curses.KEY_NPAGE:
            selected += list_h
        elif key == curses.KEY_PPAGE:
            selected -= list_h
        elif ch in ("\n", "\r") or key == curses.KEY_ENTER:
            if rows:
                return rows[selected]
        elif ch == "a":
            show_all = not show_all
            selected = 0
        elif ch == "t":
            if rows:
                s = rows[selected]
                busy(f"titling {s.session_id[:8]} with {TITLE_MODEL}")
                title = generate_title(s)
                if title:
                    apply_title(s, title, cache)
                    flash = f"✨ {title}"
                else:
                    flash = "title generation failed"
        elif ch == "s":
            if rows:
                s = rows[selected]
                if not s.summary:
                    busy(f"summarizing {s.session_id[:8]} with {TITLE_MODEL}")
                    summary = generate_summary(s)
                    if summary:
                        apply_summary(s, summary, cache)
                    else:
                        flash = "summary generation failed"
                if s.summary:
                    if not _popup(stdscr, f"{s.emoji} {s.title[:60]}", s.summary):
                        flash = "terminal too small for the summary box"


def select_rows(sessions: list[Session], show_all: bool, days: int) -> list[Session]:
    """The non-interactive equivalent of the TUI's focused view."""
    now = time.time()
    rows = sessions if show_all else [s for s in sessions if not s.small]
    if days:
        # flagged 🔖 sessions stay visible however old they are, like the TUI
        rows = [s for s in rows if s.comeback or in_window(s.mtime, days, now)]
    return rows


def print_list(sessions: list[Session], show_all: bool, days: int) -> None:
    rows = select_rows(sessions, show_all, days)
    if not rows:
        print(f"no sessions in the {window_label(days)} "
              f"(try --days 0 / --all)", file=sys.stderr)
        return
    for s in rows:
        flag = "🔖" if s.comeback else "  "
        mark = "✨" if s.generated else "  "
        pemoji, plabel, _ = PROJECTS[s.pslug]
        icon = "🤖" if s.agent else (pemoji if s.pslug != "misc" else s.emoji)
        proj = plabel if s.pslug != "misc" else s.project
        print(f"{flag} {icon} {mark} {rel_age(s.mtime):>4}  {proj[:16]:<16}  "
              f"{s.title[:60]}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clres",
        description="Browse and resume Claude Code conversations.",
        epilog="In the picker: [ and ] (or - and +) cycle the lookback window "
               "(" + " / ".join(window_label(d) for d in DAY_LADDER) + "), "
               "? shows all keys.",
    )
    p.add_argument("-d", "--days", type=int, metavar="N",
                   help=f"only show sessions touched in the last N days "
                        f"(default {DEFAULT_DAYS}; 0 = no time limit). "
                        f"🔖-flagged sessions are always kept.")
    p.add_argument("--all", action="store_true",
                   help="include tiny + headless + 🤖 subagent transcripts, "
                        "and drop the default time limit")
    p.add_argument("--index", action="store_true",
                   help=f"generate {TITLE_MODEL} titles for all untitled real convos")
    p.add_argument("--summarize", action="store_true",
                   help=f"generate {TITLE_MODEL} summaries for all real convos")
    p.add_argument("--list", action="store_true",
                   help="plain table instead of the picker (implied when piped)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable dump")
    args = p.parse_args(argv)
    if args.days is not None and args.days < 0:
        p.error("--days must be >= 0 (0 means no time limit)")
    return args


def main() -> None:
    args = parse_args()
    cache = load_cache()
    sessions = scan_sessions(cache)
    if not sessions:
        print("No Claude Code conversations found under", PROJECTS_DIR)
        sys.exit(1)
    show_all = args.all
    # An explicit --days always wins; otherwise --all drops the default limit.
    days = args.days if args.days is not None else (0 if show_all else DEFAULT_DAYS)
    if args.index:
        index_titles(sessions, cache)
        return
    if args.summarize:
        index_summaries(sessions, cache)
        return
    if args.json:
        # unfiltered by default (back-compat); --days/--all narrow it
        rows = (select_rows(sessions, show_all, days)
                if (args.days is not None or show_all) else sessions)
        print(json.dumps([s.__dict__ for s in rows], indent=2))
        return
    if args.list or not sys.stdout.isatty():
        print_list(sessions, show_all, days)
        return
    choice = curses.wrapper(run_tui, sessions, cache, show_all, days)
    if choice is not None:
        # For a 🤖 subagent row this is the parent session — what resume() opens.
        sid = choice.resume_id or choice.session_id
        print(f"{choice.emoji} resuming: {choice.title[:70]}  ({sid[:8]})")
        resume(choice)


if __name__ == "__main__":
    main()
