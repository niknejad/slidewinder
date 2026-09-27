#!/usr/bin/env python3
"""
Slidewinder - sort, edit and build LaTeX beamer slides in your browser.

    python3 slidewinder.py talk.tex

Your .tex stays the source of truth.  The sorter shows one draggable card per
frame; clicking one opens an editor with the rendered slide beside it.  Every
change - reorder, comment out, edit, split into a grid, insert, delete - backs
the file up, rewrites only the blocks it touches, reruns LaTeX and redisplays.

Pure standard library.  External programs used: a LaTeX engine (xelatex by
default) and pdftoppm from poppler, or pypdfium2 from pip instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"
WORKDIR = ".slidewinder"          # previews, thumbnails, caches and backups
OLD_WORKDIRS = (".beamer_sort",)  # earlier name; moved across on first run

# --------------------------------------------------------------------------
# LaTeX source scanning
# --------------------------------------------------------------------------

VERBATIM_ENVS = (
    "verbatim", "Verbatim", "lstlisting", "minted", "semiverbatim",
    "alltt", "comment", "listing",
)

_VERB_RE = re.compile(r"\\begin\{(" + "|".join(VERBATIM_ENVS) + r")\}")


def _verbatim_spans(src: str):
    spans = []
    for m in _VERB_RE.finditer(src):
        env = m.group(1)
        closer = "\\end{" + env + "}"
        e = src.find(closer, m.end())
        if e == -1:
            continue
        spans.append((m.start(), e + len(closer)))
    return spans


def code_mask(src: str) -> bytearray:
    """1 where a character is live LaTeX code, 0 inside comments/verbatim."""
    n = len(src)
    mask = bytearray([1]) * n
    for a, b in _verbatim_spans(src):
        for k in range(a, b):
            mask[k] = 0
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            j = src.find("\n", i)
            if j == -1:
                j = n
            for k in range(i, j):
                mask[k] = 0
            i = j + 1
            continue
        i += 1
    return mask


def find_token(src, mask, pattern, start=0, end=None):
    """First regex match at a live-code position."""
    end = len(src) if end is None else end
    for m in re.compile(pattern).finditer(src, start, end):
        if mask[m.start()]:
            return m
    return None


def iter_tokens(src, mask, pattern, start=0, end=None):
    end = len(src) if end is None else end
    for m in re.compile(pattern).finditer(src, start, end):
        if mask[m.start()]:
            yield m


def match_group(src, mask, i, opener="{", closer="}"):
    """src[i] must be `opener`; return index just past the matching closer."""
    n = len(src)
    if i >= n or src[i] != opener:
        return -1
    depth = 0
    k = i
    while k < n:
        c = src[k]
        if mask[k]:
            if c == "\\":
                k += 2
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return k + 1
        k += 1
    return -1


def skip_ws(src, i, stop_at_blank_line=True):
    n = len(src)
    newlines = 0
    while i < n and src[i] in " \t\r\n":
        if src[i] == "\n":
            newlines += 1
            if stop_at_blank_line and newlines > 1:
                return i
        i += 1
    return i


def skip_args(src, mask, i, allow=("<", "[")):
    """Skip over any run of <...> and [...] groups (and whitespace)."""
    pairs = {"<": ">", "[": "]"}
    while True:
        j = skip_ws(src, i)
        if j < len(src) and src[j] in allow and mask[j]:
            e = match_group(src, mask, j, src[j], pairs[src[j]])
            if e == -1:
                return i
            i = e
            continue
        return i


# --------------------------------------------------------------------------

class Block:
    """A movable chunk of the document body: a frame or a sectioning command.

    `disabled` blocks are the same thing commented out - every line carries a
    leading `%`, so LaTeX never sees them, but they keep their place in the deck.
    """

    __slots__ = ("id", "kind", "start", "end", "title", "line", "pages",
                 "disabled", "key")

    def __init__(self, bid, kind, start, end, title, line, disabled=False):
        self.id = bid
        self.kind = kind          # 'frame' | 'section' | 'subsection'
        self.start = start
        self.end = end
        self.title = title
        self.line = line
        self.pages = []
        self.disabled = disabled
        self.key = ""             # hash of the live text, for the thumb cache

    def as_json(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "line": self.line,
            "pages": self.pages,
            "disabled": self.disabled,
        }


# -- commenting a block out, and back in -----------------------------------

def comment_text(text: str) -> str:
    """Prefix every line with a bare `%`.  Exactly reversed by uncomment_text."""
    return "\n".join("%" + ln for ln in text.split("\n"))


def uncomment_text(text: str) -> str:
    """Drop one leading `%` from every line that has one."""
    out = []
    for ln in text.split("\n"):
        i = ln.find("%")
        if i != -1 and not ln[:i].strip():
            ln = ln[:i] + ln[i + 1:]
        out.append(ln)
    return "\n".join(out)


def is_comment_line(ln: str) -> bool:
    t = ln.lstrip()
    return t.startswith("%")


def only_comments(s: str) -> bool:
    return all((not ln.strip()) or is_comment_line(ln) for ln in s.split("\n"))


NEW_FRAME = """\\begin{frame}{New slide}
  % add content here
\\end{frame}"""


# --------------------------------------------------------------------------
# Frame structure: title + parts, where a part is either a block of text or a
# row of columns.  This is what the visual editor works on.
# --------------------------------------------------------------------------

_SKIPPABLE = re.compile(
    r"^(?:\\(?:vfill|bigskip|medskip|smallskip|par|centering)\s*"
    r"|\\v(?:space|skip)\*?\s*\{[^{}]*\}\s*"
    r"|\s+)+$")


def _blank(text: str) -> bool:
    """True for whitespace or pure vertical-spacing filler between rows."""
    return not text.strip() or bool(_SKIPPABLE.match(text))


def _columns_cells(src, mask, start, end):
    """Cells of one columns environment: [{'w': width, 'text': ...}]."""
    cells = []
    # long form: \begin{column}{w} ... \end{column}
    i = start
    while i < end:
        m = find_token(src, mask, r"\\begin\{column\}", i, end)
        if not m:
            break
        j = skip_ws(src, m.end())
        w = ""
        if j < end and src[j] == "{":
            e = match_group(src, mask, j)
            if e != -1:
                w = src[j + 1:e - 1]
                j = e
        depth = 1
        body_end = end
        for t in iter_tokens(src, mask, r"\\(begin|end)\{column\}", j, end):
            if t.group(1) == "begin":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    body_end = t.start()
                    i = t.end()
                    break
        else:
            i = end
        cells.append({"w": w, "text": src[j:body_end].strip("\n")})
    if cells:
        return cells
    # short form: \column{w} ... \column{w} ...
    marks = [m for m in iter_tokens(src, mask, r"\\column(?![a-zA-Z])", start, end)]
    for k, m in enumerate(marks):
        j = skip_ws(src, m.end())
        w = ""
        if j < end and src[j] == "{":
            e = match_group(src, mask, j)
            if e != -1:
                w = src[j + 1:e - 1]
                j = e
        stop = marks[k + 1].start() if k + 1 < len(marks) else end
        cells.append({"w": w, "text": src[j:stop].strip("\n")})
    return cells


def parse_frame(text: str):
    """Break a frame's LaTeX into {opts, title, parts}.  None if not a frame."""
    mask = code_mask(text)
    m = find_token(text, mask, r"\\begin\{frame\}")
    if not m:
        return None
    end_m = None
    for t in iter_tokens(text, mask, r"\\end\{frame\}", m.end()):
        end_m = t
    if not end_m:
        return None
    body_end = end_m.start()

    # \begin{frame}<overlay>[opts]{title}
    i = m.end()
    j = skip_args(text, mask, i, allow=("<", "["))
    opts = text[i:j].strip()
    title = ""
    j2 = skip_ws(text, j)
    if j2 < body_end and text[j2] == "{" and mask[j2]:
        e = match_group(text, mask, j2)
        if e != -1 and e <= body_end:
            title = text[j2 + 1:e - 1].strip()
            j = e
    body_start = j

    # ... or \frametitle{...} as the first thing in the body
    if not title:
        t = find_token(text, mask, r"\\frametitle(?![a-zA-Z])", body_start, body_end)
        if t and not text[body_start:t.start()].strip():
            k = skip_args(text, mask, t.end(), allow=("<",))
            k = skip_ws(text, k)
            if k < body_end and text[k] == "{":
                e = match_group(text, mask, k)
                if e != -1:
                    title = text[k + 1:e - 1].strip()
                    body_start = e

    parts = []
    pos = body_start
    for cm in iter_tokens(text, mask, r"\\begin\{columns\}", body_start, body_end):
        if cm.start() < pos:
            continue
        depth = 1
        cols_body = cm.end()
        cols_end = body_end
        for t in iter_tokens(text, mask, r"\\(begin|end)\{columns\}", cm.end(), body_end):
            if t.group(1) == "begin":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    cols_end = t.start()
                    pos_after = t.end()
                    break
        else:
            pos_after = body_end
        lead = text[pos:cm.start()]
        if not _blank(lead):
            parts.append({"kind": "text", "text": lead.strip("\n")})
        k = skip_args(text, mask, cols_body, allow=("[",))
        cols_opts = text[cols_body:k].strip()
        cells = _columns_cells(text, mask, k, cols_end)
        if cells:
            parts.append({"kind": "row", "opts": cols_opts, "cols": cells})
        else:                                   # empty columns env - keep as text
            parts.append({"kind": "text", "text": text[cm.start():pos_after].strip("\n")})
        pos = pos_after
    tail = text[pos:body_end]
    if not _blank(tail):
        parts.append({"kind": "text", "text": tail.strip("\n")})
    if not parts:
        parts = [{"kind": "text", "text": ""}]
    return {"opts": opts, "title": title, "parts": parts}


def check_block_text(text: str, kind="frame"):
    """A block's replacement text must still be exactly one block.  Returns the
    cleaned text, or raises BuildError with something the user can act on."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        raise BuildError("the text is empty")
    found = scan_blocks(text, code_mask(text), 0, len(text),
                        sections=True, subsections=True)
    if not found:
        raise BuildError(
            "that is not a complete block - it must be one "
            "\\begin{frame}...\\end{frame} (or a \\section{...})")
    if len(found) > 1:
        raise BuildError("that is %d blocks - edit one at a time, and use + to "
                         "add a new slide" % len(found))
    f = found[0]
    if text[:f["start"]].strip() or text[f["end"]:].strip():
        raise BuildError("there is text outside the block - keep it to the "
                         "frame itself")
    return text


def block_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


_CMD_CLEAN = re.compile(r"\\[a-zA-Z@]+\s*\*?")


def clean_title(s: str, limit: int = 90) -> str:
    s = re.sub(r"(?<!\\)%.*", "", s)
    s = s.replace("\\\\", " ").replace("~", " ")
    s = _CMD_CLEAN.sub(" ", s)
    s = re.sub(r"[{}$]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "\u2026"
    return s


def frame_env_end(src, mask, from_pos, limit):
    """Index just past the `\\end{frame}` closing a frame opened before from_pos."""
    depth = 1
    for t in iter_tokens(src, mask, r"\\(begin|end)\{frame\}", from_pos, limit):
        if t.group(1) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return t.end()
    return -1


def frame_title(src, mask, body_start, body_end):
    m = find_token(src, mask, r"\\frametitle(?![a-zA-Z])", body_start, body_end)
    if m:
        j = skip_args(src, mask, m.end(), allow=("<",))
        j = skip_ws(src, j)
        if j < len(src) and src[j] == "{":
            e = match_group(src, mask, j)
            if e != -1:
                return clean_title(src[j + 1:e - 1])
    # {title} directly after \begin{frame}[opts]
    j = skip_args(src, mask, body_start, allow=("<", "["))
    j = skip_ws(src, j)
    if j < body_end and src[j] == "{" and mask[j]:
        e = match_group(src, mask, j)
        if e != -1 and e <= body_end:
            t = clean_title(src[j + 1:e - 1])
            if t:
                return t
    # first line of visible text
    chunk = src[body_start:body_end]
    chunk = re.sub(r"\\end\{frame\}\s*$", "", chunk)
    chunk = re.sub(r"(?<!\\)%.*", "", chunk)
    if re.search(r"\\(titlepage|maketitle|inserttitle)\b", chunk):
        return "title page"
    if re.search(r"\\tableofcontents\b", chunk):
        return "outline"
    for line in chunk.splitlines():
        t = clean_title(line, 60)
        if t:
            return t
    return ""


_BLOCK_RE = re.compile(r"\\begin\{frame\}"
                       r"|\\frame(?![a-zA-Z])"
                       r"|\\(sub)?section(?![a-zA-Z])\*?")


def scan_blocks(src, mask, start, end, sections=True, subsections=False,
                warnings=None):
    """Movable blocks in src[start:end], as {kind,start,end,title} dicts."""
    out = []
    i = start
    while i < end:
        m = _BLOCK_RE.search(src, i, end)
        if not m:
            break
        if not mask[m.start()]:
            i = m.start() + 1
            continue
        tok = m.group(0)
        if tok.startswith("\\begin{frame}"):
            b_end = frame_env_end(src, mask, m.end(), end)
            if b_end == -1:
                if warnings is not None:
                    warnings.append("unterminated \\begin{frame} at line %d"
                                    % (src.count("\n", 0, m.start()) + 1))
                i = m.end()
                continue
            out.append({"kind": "frame", "start": m.start(), "end": b_end,
                        "title": frame_title(src, mask, m.end(), b_end)})
            i = b_end
            continue
        if tok.startswith("\\frame"):
            j = skip_args(src, mask, m.end(), allow=("<", "["))
            j = skip_ws(src, j)
            if j < len(src) and src[j] == "{":
                b_end = match_group(src, mask, j)
                if b_end != -1:
                    out.append({"kind": "frame", "start": m.start(), "end": b_end,
                                "title": frame_title(src, mask, j + 1, b_end)})
                    i = b_end
                    continue
            i = m.end()
            continue
        is_sub = bool(m.group(1))
        kind = "subsection" if is_sub else "section"
        wanted = subsections if is_sub else sections
        j = skip_args(src, mask, m.end(), allow=("[",))
        j = skip_ws(src, j)
        if j >= len(src) or src[j] != "{":
            i = m.end()
            continue
        b_end = match_group(src, mask, j)
        if b_end == -1:
            i = m.end()
            continue
        if wanted:
            out.append({"kind": kind, "start": m.start(), "end": b_end,
                        "title": clean_title(src[j + 1:b_end - 1])})
        i = b_end
    return out


class TexDoc:
    """The parsed presentation."""

    def __init__(self, path: Path, want_sections=True, want_subsections=False):
        self.path = path
        self.src = path.read_text(encoding="utf-8", errors="surrogateescape")
        self.mask = code_mask(self.src)
        self.want_sections = want_sections
        self.want_subsections = want_subsections
        self.warnings = []
        self._locate_body()
        self._find_blocks()

    # -- structure ---------------------------------------------------------
    def _locate_body(self):
        m = find_token(self.src, self.mask, r"\\begin\{document\}")
        if not m:
            raise ValueError("no \\begin{document} found - is this a LaTeX file?")
        self.preamble_end = m.start()
        self.body_start = m.end()
        e = None
        for t in iter_tokens(self.src, self.mask, r"\\end\{document\}", self.body_start):
            e = t
        self.body_end = e.start() if e else len(self.src)

    def _line_of(self, pos):
        return self.src.count("\n", 0, pos) + 1

    def _find_blocks(self):
        found = scan_blocks(self.src, self.mask, self.body_start, self.body_end,
                            self.want_sections, self.want_subsections, self.warnings)
        blocks = [Block(0, f["kind"], f["start"], f["end"], f["title"],
                        self._line_of(f["start"])) for f in found]
        blocks += self._find_disabled()
        blocks.sort(key=lambda b: b.start)
        for i, b in enumerate(blocks):
            b.id = i
            b.key = block_key(self.live_text(b))
        self.blocks = blocks

    def live_text(self, b: Block) -> str:
        """The block's source as LaTeX would see it (uncommented if disabled)."""
        t = self.src[b.start:b.end]
        return uncomment_text(t) if b.disabled else t

    def _find_disabled(self):
        """Runs of commented-out lines that parse as a frame or section."""
        src = self.src
        base, end = self.body_start, self.body_end
        body = src[base:end]
        vspans = [(a, b) for a, b in _verbatim_spans(src) if b > base and a < end]

        def in_verbatim(pos):
            return any(a <= pos < b for a, b in vspans)

        # line table for the body
        starts = [base]
        for m in re.finditer("\n", body):
            starts.append(base + m.end())
        ends = []
        for s in starts:
            nl = src.find("\n", s, end)
            ends.append(nl if nl != -1 else end)

        out = []
        i, n = 0, len(starts)
        while i < n:
            if not is_comment_line(src[starts[i]:ends[i]]) or in_verbatim(starts[i]):
                i += 1
                continue
            j = i
            while (j + 1 < n and is_comment_line(src[starts[j + 1]:ends[j + 1]])
                   and not in_verbatim(starts[j + 1])):
                j += 1
            out += self._disabled_in_run(starts[i:j + 1], ends[i:j + 1])
            i = j + 1
        return out

    def _disabled_in_run(self, line_starts, line_ends):
        """Parse one run of comment lines; return the Blocks hiding in it."""
        run = self.src[line_starts[0]:line_ends[-1]]
        inner = uncomment_text(run)
        if "frame" not in inner and "section" not in inner:
            return []
        imask = code_mask(inner)
        found = scan_blocks(inner, imask, 0, len(inner),
                            self.want_sections, self.want_subsections)
        if not found:
            return []
        # uncommenting is line-preserving, so inner line k == run line k
        out = []
        for f in found:
            lo = inner.count("\n", 0, f["start"])
            hi = inner.count("\n", 0, max(f["start"], f["end"] - 1))
            if hi >= len(line_starts):
                continue
            # the commented block must own its lines outright
            head = inner[:f["start"]].rsplit("\n", 1)[-1]
            tail = inner[f["end"]:].split("\n", 1)[0]
            if not only_comments(head) and head.strip():
                continue
            if tail.strip() and not only_comments(tail):
                continue
            out.append(Block(0, f["kind"], line_starts[lo], line_ends[hi],
                             f["title"], self._line_of(line_starts[lo]),
                             disabled=True))
        return out

    # -- rewriting ---------------------------------------------------------
    def instrumented(self) -> str:
        """Source with per-block markers, for the preview build."""
        out = [self.src[:self.preamble_end], INSTRUMENT, self.src[self.preamble_end:]]
        text = "".join(out)
        shift = len(INSTRUMENT)
        pieces = []
        prev = 0
        for b in self.blocks:
            s = b.start + shift
            pieces.append(text[prev:s])
            pieces.append("\\bsblk{%d}%%\n" % (b.id + 1))
            prev = s
        pieces.append(text[prev:])
        return "".join(pieces)

    def rewrite(self, order, disabled=(), edits=None, inserts=None,
                deletes=()) -> str:
        """Source with the blocks permuted, commented in or out, edited, and
        with new blocks inserted.  Everything between blocks stays anchored.

        edits:   {block id: replacement LaTeX (uncommented form)}
        inserts: [{"after": block id or None (= before everything), "text": ...}]
        """
        edits = edits or {}
        inserts = inserts or []
        if not self.blocks:
            return self.src
        blocks = self.blocks
        want_off = set(disabled)
        after_map = {}
        for ins in inserts:
            after_map.setdefault(ins.get("after"), []).append(ins["text"])

        def body(bid):
            b = blocks[bid]
            text = edits[bid] if bid in edits else self.live_text(b)
            return comment_text(text) if bid in want_off else text

        gone = set(deletes)
        kept = [bid for bid in order if bid not in gone]
        out = [self.src[:blocks[0].start]]
        for text in after_map.get(None, []):
            out.append(text.rstrip("\n") + "\n\n")
        for slot, bid in enumerate(kept):
            out.append(body(bid))
            for text in after_map.get(bid, []):
                out.append("\n\n" + text.rstrip("\n"))
            if slot < len(kept) - 1:
                out.append(self.src[blocks[slot].end:blocks[slot + 1].start])
        # inserts hung off a deleted block still have to go somewhere
        for bid in order:
            if bid in gone:
                for text in after_map.get(bid, []):
                    out.append("\n\n" + text.rstrip("\n"))
        out.append(self.src[blocks[-1].end:])
        return "".join(out)

    # kept for callers that only reorder
    def reordered(self, order, disabled=()) -> str:
        return self.rewrite(order, disabled)


INSTRUMENT = r"""
%%% --- slidewinder instrumentation (temporary preview copy only) ---
\makeatletter
\newwrite\bs@out
\newcounter{bsblk}
\setcounter{bsblk}{0}
\newcommand{\bsblk}[1]{\setcounter{bsblk}{#1}}
\AtBeginDocument{\immediate\openout\bs@out=\jobname.bsmap\relax}
\AtEndDocument{\immediate\closeout\bs@out}
\makeatother
\usepackage{atbegshi}
\makeatletter
\AtBeginShipout{\immediate\write\bs@out{\thebsblk}}
\makeatother
%%% --- end slidewinder instrumentation ---
"""


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

class BuildError(Exception):
    pass


# -- PDF rendering: poppler's pdftoppm if present, else pypdfium2 from pip ---

_PDFIUM = None

# PDFium is NOT thread-safe and this server is threaded: opening the image
# picker fires several thumbnail requests at once.  Concurrent PdfDocument
# open/render/close calls corrupt the heap and take the whole process down with
# a malloc error or a segfault, so every use of pypdfium2 here holds this lock.
_PDFIUM_LOCK = threading.RLock()

FORCE_RENDERER = None            # set by --renderer


def pdfium():
    """The pypdfium2 module, or False if it is not installed."""
    global _PDFIUM
    if _PDFIUM is None:
        try:
            import pypdfium2
            _PDFIUM = pypdfium2
        except Exception:                                   # noqa: BLE001
            _PDFIUM = False
    return _PDFIUM


def have_pdftoppm():
    if FORCE_RENDERER == "pypdfium2":
        return False
    return bool(shutil.which("pdftoppm"))


def renderer_name():
    if have_pdftoppm():
        return "pdftoppm"
    if FORCE_RENDERER != "pdftoppm" and pdfium():
        return "pypdfium2"
    return None


NO_RENDERER = (
    "no PDF renderer found.\n"
    "  Either install poppler      (macOS: brew install poppler)\n"
    "  or create the virtualenv    ./setup.sh   then   source .venv/bin/activate"
)


def render_pdf(pdf: Path, outdir: Path, prefix: str, width: int,
               first=None, last=None, clean=False):
    """Render pages of `pdf` to outdir/<tag>-<page>.png.  Returns {page: path}.

    `clean` wipes earlier output for this prefix first and keeps the prefix as
    the file name - only the build thread, which is serialised, may use it.
    Everybody else gets a unique tag, so two renders running at once can never
    read or delete each other's files.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    if clean:
        for stale in outdir.glob(prefix + "-*.png"):
            stale.unlink(missing_ok=True)
        tag = prefix
    else:
        tag = "%s%s" % (prefix, uuid.uuid4().hex[:8])

    if have_pdftoppm():
        cmd = ["pdftoppm", "-png", "-scale-to-x", str(width), "-scale-to-y", "-1"]
        if first:
            cmd += ["-f", str(first)]
        if last:
            cmd += ["-l", str(last)]
        cmd += [str(pdf), str(outdir / tag)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=600)
        out = {}
        for p in outdir.glob(tag + "-*.png"):
            m = re.search(r"-(\d+)\.png$", p.name)
            if m:
                out[int(m.group(1))] = p
        return out

    pdfium_mod = pdfium()
    if not pdfium_mod:
        raise BuildError(NO_RENDERER)
    out = {}
    with _PDFIUM_LOCK:                       # see the note on _PDFIUM_LOCK
        doc = pdfium_mod.PdfDocument(str(pdf))
        try:
            lo = first or 1
            hi = last or len(doc)
            for i in range(lo, min(hi, len(doc)) + 1):
                page = doc[i - 1]
                scale = max(0.1, width / float(page.get_width()))
                img = page.render(scale=scale).to_pil()
                path = outdir / ("%s-%d.png" % (tag, i))
                img.save(str(path))
                out[i] = path
        finally:
            try:
                doc.close()
            except Exception:                               # noqa: BLE001
                pass
    return out


class Project:
    def __init__(self, tex: Path, engine="xelatex", passes=2, dpi=None,
                 sections=True, subsections=False):
        self.tex = tex.resolve()
        self.dir = self.tex.parent
        self.engine = engine
        self.passes = passes
        self.thumb_width = dpi or 640
        self.sections = sections
        self.subsections = subsections

        # one work area per .tex, so two decks in the same folder never share
        # a preview PDF, thumbnails or a cache
        for old in OLD_WORKDIRS:                  # keep older backups reachable
            if (self.dir / old).is_dir() and not (self.dir / WORKDIR).exists():
                try:
                    (self.dir / old).rename(self.dir / WORKDIR)
                except OSError:
                    pass
        self.work = self.dir / WORKDIR / re.sub(r"[^\w.-]", "_", self.tex.stem)
        self.build = self.work / "build"
        self.thumbs = self.work / "thumbs"
        self.pages = self.work / "pages"
        self.backups = self.work / "backups"
        self.cache = self.work / "cache"
        self.imgcache = self.work / "imgcache"
        for d in (self.build, self.thumbs, self.pages, self.backups, self.cache,
                  self.imgcache):
            d.mkdir(parents=True, exist_ok=True)

        self.lock = threading.Lock()
        self.run_id = uuid.uuid4().hex[:8]   # makes image URLs unique per run
        self.build_id = 0
        self.building = False
        self.ok = False
        self.error = None
        self.log = ""
        self.doc = None
        self.front_pages = []
        self.npages = 0
        self.last_build_s = 0.0
        self.message = ""
        self.map_entries = 0
        self.verbose = False

    # -- state -------------------------------------------------------------
    @property
    def stamp(self):
        """Unique per run and per build - the cache buster in image URLs."""
        return "%s.%d" % (self.run_id, self.build_id)

    def block_thumb(self, b):
        """URL for this block's picture: live page, or the cached last look."""
        if not b.disabled and b.pages:
            return "thumb/%s/%d.png" % (self.stamp, b.pages[0])
        if b.key and (self.cache / (b.key + ".png")).exists():
            return "cache/%s/%s.png" % (self.run_id, b.key)
        return None

    def state(self):
        blocks = []
        for b in (self.doc.blocks if self.doc else []):
            j = b.as_json()
            j["thumb"] = self.block_thumb(b)
            blocks.append(j)
        return {
            "file": str(self.tex),
            "name": self.tex.name,
            "build": self.build_id,
            "stamp": self.stamp,
            "building": self.building,
            "ok": self.ok,
            "error": self.error,
            "log": self.log[-8000:],
            "message": self.message,
            "blocks": blocks,
            "order": [b["id"] for b in blocks],
            "front": self.front_pages,
            "npages": self.npages,
            "hidden": sum(1 for b in (self.doc.blocks if self.doc else []) if b.disabled),
            "seconds": round(self.last_build_s, 1),
            "warnings": (self.doc.warnings if self.doc else []),
            "backups": sorted(p.name for p in self.backups.glob("*.bak"))[-20:],
        }

    # -- build -------------------------------------------------------------
    def rebuild_async(self, message=""):
        def run():
            try:
                self.rebuild(message)
            except Exception as exc:                      # noqa: BLE001
                self.error = str(exc)
                self.ok = False
            finally:
                self.building = False
        if self.building:
            return False
        self.building = True
        self.error = None
        self.message = message
        threading.Thread(target=run, daemon=True).start()
        return True

    def rebuild(self, message=""):
        t0 = time.time()
        with self.lock:
            self.doc = TexDoc(self.tex, self.sections, self.subsections)
            prev = self.build / "preview.tex"
            prev.write_text(self.doc.instrumented(), encoding="utf-8",
                            errors="surrogateescape")
            for stale in (self.build / "preview.pdf", self.build / "preview.bsmap"):
                stale.unlink(missing_ok=True)

            log = []
            rc = 0
            for n in range(max(1, self.passes)):
                rc, out = self._run_engine(prev)
                log.append(out)
                if rc != 0:
                    break
            self.log = "\n".join(log)
            pdf = self.build / "preview.pdf"
            if not pdf.exists():
                self.ok = False
                self.error = "%s failed - see the log below" % self.engine
                self.build_id += 1
                self.last_build_s = time.time() - t0
                return
            if rc != 0:
                self.error = "%s reported errors; showing the PDF it managed to write" % self.engine

            self._map_pages()
            self._thumbnails(pdf)
            self._cache_thumbs()
            if self.verbose:
                print("  page map: %d entries for %d pages" %
                      (self.map_entries, self.npages))
                if self.front_pages:
                    print("    %-28s %s" % ("(front matter)", self.front_pages))
                for b in self.doc.blocks:
                    print("    %-28s %s%s" % (b.title[:28] or b.kind, b.pages,
                                              "  [commented out]" if b.disabled else ""))
            self.ok = True
            self.build_id += 1
            self.last_build_s = time.time() - t0

    def _run_engine(self, texfile: Path):
        rel = os.path.relpath(texfile, self.dir)
        cmd = [self.engine, "-interaction=nonstopmode", "-file-line-error",
               "-output-directory=" + os.path.relpath(self.build, self.dir), rel]
        try:
            p = subprocess.run(cmd, cwd=str(self.dir), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise BuildError("%s not found on PATH" % self.engine)
        except subprocess.TimeoutExpired:
            raise BuildError("%s timed out after 10 minutes" % self.engine)
        out = p.stdout.decode("utf-8", "replace")
        return p.returncode, _interesting(out)

    def _map_pages(self):
        """Attribute each PDF page to the block that produced it."""
        bsmap = self.build / "preview.bsmap"
        ids = []
        if bsmap.exists():
            for line in bsmap.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.isdigit():
                    ids.append(int(line))
        self.npages = _pdf_pages(self.build / "preview.pdf") or len(ids)
        self.map_entries = len(ids)
        for b in self.doc.blocks:
            b.pages = []
        self.front_pages = []
        live = [b for b in self.doc.blocks if not b.disabled]
        if ids:
            if len(ids) != self.npages:
                self.doc.warnings.append(
                    "page map has %d entries for %d PDF pages - some thumbnails "
                    "may belong to the wrong frame (run with --verbose to see the map)"
                    % (len(ids), self.npages))
            for page in range(1, self.npages + 1):
                bid = ids[page - 1] if page <= len(ids) else 0
                if 1 <= bid <= len(self.doc.blocks):
                    self.doc.blocks[bid - 1].pages.append(page)
                else:
                    self.front_pages.append(page)
        else:
            # no map at all: assume one page per live block, in order
            if self.npages:
                self.doc.warnings.append(
                    "no page map was written - assuming one page per frame")
            for i, b in enumerate(live):
                if i + 1 <= self.npages:
                    b.pages = [i + 1]

    def _thumbnails(self, pdf: Path):
        for old in self.pages.glob("*.png"):
            old.unlink(missing_ok=True)
        # only the build thread reaches this, and it holds self.lock
        self.thumb_files = render_pdf(pdf, self.thumbs, "t", self.thumb_width,
                                      clean=True)

    def _cache_thumbs(self, keep=600):
        """Remember what each live block looked like, so a hidden one can still
        show its picture after it stops being compiled."""
        for b in self.doc.blocks:
            if b.disabled or not b.pages or not b.key:
                continue
            src = self.thumb_files.get(b.pages[0])
            dst = self.cache / (b.key + ".png")
            if src and not dst.exists():
                try:
                    shutil.copyfile(src, dst)
                except OSError:
                    pass
        files = sorted(self.cache.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            old.unlink(missing_ok=True)

    def thumb(self, page: int):
        return getattr(self, "thumb_files", {}).get(page)

    def big_page(self, page: int):
        out = self.pages / ("p-%04d.png" % page)
        if out.exists():
            return out
        pdf = self.build / "preview.pdf"
        if not pdf.exists():
            return None
        got = render_pdf(pdf, self.pages, "tmp", 1400, first=page, last=page)
        for p in got.values():
            p.replace(out)
            return out
        return None if not out.exists() else out

    # -- writing -----------------------------------------------------------
    def apply_order(self, order, disabled=(), edits=None, inserts=None,
                    deletes=None):
        doc = self.doc
        if doc is None:
            raise BuildError("nothing parsed yet")
        n = len(doc.blocks)
        if sorted(order) != list(range(n)):
            raise BuildError("bad ordering (expected a permutation of %d blocks)" % n)
        want_off = set(disabled)
        if any(b not in range(n) for b in want_off):
            raise BuildError("unknown block id in the hidden list")

        clean_edits = {}
        for k, v in (edits or {}).items():
            bid = int(k)
            if bid not in range(n):
                raise BuildError("unknown block id %d" % bid)
            text = check_block_text(v, doc.blocks[bid].kind)
            if text != doc.live_text(doc.blocks[bid]):
                clean_edits[bid] = text
        clean_inserts = []
        for ins in (inserts or []):
            after = ins.get("after")
            after = None if after in (None, "", -1, "-1") else int(after)
            if after is not None and after not in range(n):
                raise BuildError("cannot insert after unknown block %s" % after)
            clean_inserts.append({"after": after,
                                  "text": check_block_text(ins.get("text") or
                                                           self.new_frame())})

        gone = set(int(x) for x in (deletes or []))
        if any(b not in range(n) for b in gone):
            raise BuildError("unknown block id in the delete list")

        same_order = order == list(range(n))
        same_state = want_off == {b.id for b in doc.blocks if b.disabled}
        if (same_order and same_state and not clean_edits and not clean_inserts
                and not gone):
            return None, []

        # where the inserted blocks will land once the file is re-parsed:
        # ids are assigned in document order, so walk the sequence we are about
        # to write out
        after_map = {}
        for ins in clean_inserts:
            after_map.setdefault(ins["after"], []).append(ins["text"])
        seq = ["new"] * len(after_map.get(None, []))
        for bid in order:
            if bid not in gone:
                seq.append("old")
            seq += ["new"] * len(after_map.get(bid, []))
        new_ids = [i for i, kind in enumerate(seq) if kind == "new"]

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.backups / ("%s.%s.bak" % (self.tex.name, stamp))
        shutil.copy2(self.tex, backup)
        new_src = doc.rewrite(order, want_off, clean_edits, clean_inserts, gone)
        tmp = self.tex.with_suffix(self.tex.suffix + ".tmp")
        tmp.write_text(new_src, encoding="utf-8", errors="surrogateescape")
        tmp.replace(self.tex)
        return backup, new_ids

    def new_frame(self):
        """Template for an inserted slide: <work>/newslide.tex if you made one."""
        tpl = self.work / "newslide.tex"
        try:
            if tpl.exists():
                text = tpl.read_text(encoding="utf-8")
                if text.strip():
                    return text
        except OSError:
            pass
        return NEW_FRAME

    def block_source(self, bid):
        doc = self.doc
        if doc is None or bid not in range(len(doc.blocks)):
            raise BuildError("no such block")
        b = doc.blocks[bid]
        text = doc.live_text(b)
        out = {"id": bid, "kind": b.kind, "title": b.title,
               "disabled": b.disabled, "text": text, "pages": b.pages,
               "thumb": self.block_thumb(b)}
        out["frame"] = parse_frame(text) if b.kind == "frame" else None
        return out

    # -- images ------------------------------------------------------------
    IMG_EXT = (".png", ".jpg", ".jpeg", ".pdf", ".gif", ".svg", ".webp",
               ".eps", ".ps", ".tif", ".tiff")
    SHOWABLE = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

    def images(self, limit=500):
        """Image files near the deck, as LaTeX would reference them."""
        root = self.dir
        skip = {WORKDIR, ".git", "node_modules", "__pycache__", ".venv"}
        out = []
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in skip and not d.startswith(".")]
            if len(Path(dirpath).relative_to(root).parts) > 4:
                dirnames[:] = []
                continue
            for f in files:
                if not f.lower().endswith(self.IMG_EXT):
                    continue
                p = Path(dirpath) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append({"path": p.relative_to(root).as_posix(),
                            "size": st.st_size, "mtime": st.st_mtime,
                            "shows": p.suffix.lower() in self.SHOWABLE
                                     or p.suffix.lower() == ".pdf"})
            if len(out) > limit * 2:
                break
        out.sort(key=lambda d: -d["mtime"])
        return out[:limit]

    def _safe_rel(self, rel):
        p = (self.dir / rel).resolve()
        if not str(p).startswith(str(self.dir.resolve()) + os.sep):
            raise BuildError("outside the deck folder")
        return p

    def image_thumb(self, rel):
        p = self._safe_rel(rel)
        if not p.exists():
            return None
        ext = p.suffix.lower()
        if ext in self.SHOWABLE:
            return p
        if ext == ".pdf":
            out = self.imgcache / (block_key(str(p) + str(p.stat().st_mtime)) + ".png")
            if not out.exists():
                got = render_pdf(p, self.imgcache, "tmpimg", 400, first=1, last=1)
                for f in got.values():
                    try:
                        f.replace(out)
                    except OSError:
                        pass
                    break
            return out if out.exists() else None
        return None

    def save_upload(self, name, data: bytes, folder="figures"):
        name = os.path.basename(name or "image.png").strip()
        name = re.sub(r"[^\w.\- ]", "_", name) or "image.png"
        if not name.lower().endswith(self.IMG_EXT):
            raise BuildError("%s is not an image file" % name)
        dest_dir = self.dir / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        stem, ext = os.path.splitext(name)
        n = 1
        while dest.exists():
            dest = dest_dir / ("%s-%d%s" % (stem, n, ext))
            n += 1
        dest.write_bytes(data)
        return dest.relative_to(self.dir).as_posix()

    def revert(self, name=None):
        baks = sorted(self.backups.glob("*.bak"))
        if not baks:
            raise BuildError("no backups yet")
        src = self.backups / name if name else baks[-1]
        if not src.exists():
            raise BuildError("no such backup: %s" % name)
        shutil.copy2(src, self.tex)
        return src


def _pdf_pages(pdf: Path):
    if not pdf.exists():
        return 0
    mod = pdfium()
    if mod:
        try:
            with _PDFIUM_LOCK:
                doc = mod.PdfDocument(str(pdf))
                n = len(doc)
                doc.close()
            return n
        except Exception:                                   # noqa: BLE001
            pass
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60).stdout.decode(
                                 "utf-8", "replace")
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        if m:
            return int(m.group(1))
    except FileNotFoundError:
        pass
    data = pdf.read_bytes()
    return max(data.count(b"/Type /Page\n"), data.count(b"/Type/Page"), 0)


_KEEP = re.compile(r"(^!|^l\.\d|error|Error|Warning|Overfull|Underfull|"
                   r"^\S+\.tex:\d+:|Output written|undefined)")


def _interesting(log: str) -> str:
    lines = log.splitlines()
    keep = []
    hold = 0
    for ln in lines:
        if _KEEP.search(ln):
            keep.append(ln)
            hold = 3
        elif hold:
            keep.append(ln)
            hold -= 1
    return "\n".join(keep) if keep else "\n".join(lines[-40:])


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__ - Slidewinder</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#15181d; --muted:#6b7280; --line:#e3e6ea;
  --accent:#3b6df6; --accent-soft:#e8efff; --warn:#b45309; --err:#b91c1c; --card:190px;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#111317; --panel:#181b21; --ink:#e8eaee; --muted:#98a0ad; --line:#272b33;
         --accent:#6f9bff; --accent-soft:#1e2740; --warn:#e0a758; --err:#ff8085; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
 padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0 8px 0 0;font-weight:650;letter-spacing:.2px}
.file{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:12px}
.grow{flex:1}
button{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);
 background:var(--panel);color:var(--ink);cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:disabled{background:var(--muted);border-color:var(--muted)}
input[type=range]{width:120px;vertical-align:middle}
main{padding:16px}
#grid{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}
.card{width:var(--card);background:var(--panel);border:1px solid var(--line);border-radius:10px;
 overflow:hidden;cursor:grab;user-select:none;transition:box-shadow .12s,transform .12s,border-color .12s}
.card:hover{border-color:var(--accent);box-shadow:0 4px 14px rgba(0,0,0,.10)}
.card.drag{opacity:.55;border-color:var(--accent);border-style:dashed;
 box-shadow:0 0 0 3px var(--accent-soft)}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
/* while dragging: the two slides the dragged one will land between */
.card.nbr{border-color:var(--accent)}
.card.nbr-l{box-shadow:inset -5px 0 0 var(--accent)}
.card.nbr-r{box-shadow:inset 5px 0 0 var(--accent)}
.card.nbr .meta{background:var(--accent-soft)}
.drop{color:var(--accent);font-weight:600}
.card.locked{cursor:default;opacity:.85;border-style:dashed}
.card.off{border-style:dashed;background:transparent}
.card.off .thumb{filter:grayscale(1) contrast(.75);opacity:.4}
.card.off .thumb.empty{display:flex;align-items:center;justify-content:center;
 aspect-ratio:var(--ar,4/3);filter:none;opacity:1;color:var(--muted);
 background:repeating-linear-gradient(45deg,var(--line) 0 6px,transparent 6px 12px)}
.card.off .ttl{text-decoration:line-through;color:var(--muted)}
.acts{margin-left:auto;display:flex;gap:1px;flex:none}
.act{border:0;background:transparent;color:var(--muted);
 padding:1px 3px;font-size:13px;line-height:1;border-radius:4px;cursor:pointer}
.act:hover{background:var(--accent-soft);color:var(--accent)}
.card.off .badge{background:var(--muted)}
.card.section .thumb{display:flex;align-items:center;justify-content:center;
 background:linear-gradient(135deg,var(--accent-soft),transparent);aspect-ratio:var(--ar,4/3);
 padding:14px 10px;text-align:center;font-weight:600;font-size:13px;overflow:hidden}
.thumb{position:relative;background:#fff;border-bottom:1px solid var(--line)}
.thumb img{display:block;width:100%;height:auto}
.badge{position:absolute;top:5px;right:5px;background:rgba(0,0,0,.66);color:#fff;
 border-radius:5px;font-size:10px;padding:1px 5px;letter-spacing:.3px}
.meta{padding:6px 8px;display:flex;gap:6px;align-items:baseline}
.num{font-variant-numeric:tabular-nums;color:var(--muted);font-size:11px;min-width:1.6em}
.ttl{flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kind{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
#status{padding:0 16px 8px;color:var(--muted);font-size:12px}
.err{color:var(--err)} .warn{color:var(--warn)}
pre#log{margin:8px 16px 24px;padding:10px 12px;background:var(--panel);border:1px solid var(--line);
 border-radius:8px;max-height:260px;overflow:auto;font-size:11.5px;white-space:pre-wrap;color:var(--muted)}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;z-index:50;
 flex-direction:column;gap:10px;padding:16px;overflow:auto;
 align-items:flex-start;justify-content:flex-start}
/* margin:auto centres it while small without making the overflow unreachable
   once it is zoomed past the window, which align-items:center would do */
#overlay img{background:#fff;border-radius:6px;flex:none;margin:auto}
#overlay img.fit{max-width:96vw;max-height:84vh}
#overlay img.zoom{max-width:none;max-height:none}
#overlay .cap{color:#e8eaee;font-size:13px;display:flex;align-items:center;gap:8px;
 flex:none;position:sticky;bottom:0;left:0;margin:0 auto;
 background:rgba(20,20,24,.86);padding:7px 12px;border-radius:9px;
 backdrop-filter:blur(3px)}
#overlay .cap button{background:rgba(255,255,255,.14);border-color:transparent;color:#fff;
 padding:3px 10px}
#overlay .cap button:hover{background:rgba(255,255,255,.3);border-color:transparent}
#editor,#picker,#ask{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
 z-index:60;align-items:center;justify-content:center;padding:20px}
#picker{z-index:70} #ask{z-index:80}
#editor .panel,#picker .panel,#ask .panel{background:var(--panel);
 border:1px solid var(--line);border-radius:12px;display:flex;flex-direction:column;
 box-shadow:0 20px 60px rgba(0,0,0,.35);overflow:hidden}
#editor .panel{width:min(1500px,98vw);height:min(940px,94vh)}
#picker .panel{width:min(900px,92vw);max-height:80vh}
#ask .panel{width:min(420px,92vw)}
.askmsg{padding:18px 18px 4px;line-height:1.5}
#editor .bar,#picker .bar,#ask .bar{display:flex;align-items:center;gap:8px;
 padding:8px 12px;border-bottom:1px solid var(--line);flex:none}
#editor .bar:last-child,#picker .bar:last-child,#ask .bar{border-bottom:0;
 border-top:1px solid var(--line)}
.tabs{display:flex;margin-left:6px}
.tab{border-radius:0;border:1px solid var(--line);margin:0 0 0 -1px;padding:4px 12px;
 color:var(--muted)}
.tab:first-child{border-radius:7px 0 0 7px} .tab:last-child{border-radius:0 7px 7px 0}
.tab.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent);
 position:relative;z-index:1}
.edbody{flex:1;display:flex;min-height:0}
.edmain{flex:1;display:flex;flex-direction:column;min-width:320px;min-height:0}
.edside{width:var(--side,340px);flex:none;padding:12px;
 display:flex;flex-direction:column;gap:8px;overflow:auto}
/* drag this to make the preview as big as you need */
.grip{flex:none;width:7px;cursor:col-resize;background:var(--line);
 background-clip:content-box;padding:0 3px;box-sizing:border-box;
 border-left:1px solid var(--line);border-right:1px solid var(--line)}
.grip:hover,.grip.on{background:var(--accent)}
.prevbox{border:1px solid var(--line);border-radius:8px;background:#fff;overflow:hidden;
 min-height:60px;display:flex;align-items:center;justify-content:center}
.prevbox img{width:100%;height:auto;display:block}
#vis{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:10px;
 min-height:0}
.toolbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.toolbar .sep{width:1px;height:20px;background:var(--line);margin:0 4px}
.toolbar input[type=number]{width:52px;padding:5px 6px;border-radius:6px;
 border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit}
.titlefld{flex:1;min-width:160px;padding:6px 10px;border-radius:7px;font:inherit;
 border:1px solid var(--line);background:var(--panel);color:var(--ink)}
button.t{padding:5px 9px;min-width:34px}
/* the parts stack fills the editor, so a 2x2 grid of boxes looks like the slide */
#parts{flex:1;display:flex;flex-direction:column;gap:12px;min-height:0}
.part{border:1px solid var(--line);border-radius:9px;overflow:hidden;
 flex:1;display:flex;flex-direction:column;min-height:150px}
.phdr{display:flex;align-items:center;gap:6px;padding:4px 8px;background:var(--bg);
 border-bottom:1px solid var(--line);font-size:11px;color:var(--muted)}
.phdr button{padding:2px 7px;font-size:11px}
.cells{display:flex;gap:0;flex:1;min-height:0}
.cellwrap{flex:1;min-width:0;border-right:1px dashed var(--line);display:flex;
 flex-direction:column;min-height:0}
.cellwrap:last-child{border-right:0}
.cellw{font-size:10px;color:var(--muted);padding:2px 6px 0;text-align:right}
textarea.cell,#edtext{border:0;outline:0;resize:vertical;padding:10px;width:100%;
 font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;tab-size:2;
 background:var(--panel);color:var(--ink)}
textarea.cell{flex:1;min-height:90px;resize:none}
textarea.cell:focus{background:var(--accent-soft)}
textarea.cell.drop,#edtext.drop{outline:2px dashed var(--accent);outline-offset:-4px}
#edtext{flex:1;min-height:0;height:100%}
#pkgrid{flex:1;overflow:auto;padding:12px;display:flex;flex-wrap:wrap;gap:10px;
 align-content:flex-start;min-height:140px}
#pkgrid.drop{outline:2px dashed var(--accent);outline-offset:-6px}
.pk{width:150px;border:1px solid var(--line);border-radius:8px;overflow:hidden;
 cursor:pointer;background:var(--panel)}
.pk:hover{border-color:var(--accent)}
.pk .im{height:96px;display:flex;align-items:center;justify-content:center;
 background:#fff;overflow:hidden}
.pk .im img{max-width:100%;max-height:96px;display:block}
.pk .nm{font-size:11px;padding:4px 6px;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap;direction:rtl;text-align:left}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--muted);
 border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-1px}
@keyframes s{to{transform:rotate(360deg)}}
.hint{color:var(--muted);font-size:12px}
</style></head><body>
<header>
  <h1>Slidewinder</h1><span class="file" id="fname">__NAME__</span>
  <span class="grow"></span>
  <button id="newbtn" title="insert a new slide after the selected one (n)">+ Slide</button>
  <span class="hint" title="click a slide to edit it. keys: v view &middot; x comment out &middot; n new slide &middot; del delete">click to edit &middot; v view &middot; x hide &middot; n new</span>
  <span class="hint">size</span><input type="range" id="zoom" min="120" max="420" step="10" value="190">
  <a href="preview.pdf" id="pdflink" target="_blank" class="hint" style="text-decoration:none">pdf</a>
  <button id="logbtn">Log</button>
  <button id="reset">Reset order</button>
  <button id="revert" title="Restore the most recent backup">Revert</button>
  <button id="rebuild">Rebuild</button>
  <button id="apply" class="primary" disabled>Apply &amp; rebuild</button>
</header>
<div id="status"></div>
<main><div id="grid"></div></main>
<pre id="log" hidden></pre>
<div id="overlay">
  <img id="big" class="fit" alt="">
  <div class="cap">
    <span id="cap"></span>
    <button id="zout" title="smaller (-)">&minus;</button>
    <button id="zfit" title="fit the window (0)">fit</button>
    <button id="zin" title="bigger (+)">+</button>
    <button id="vclose" title="close (Esc)">close</button>
  </div>
</div>
<div id="editor">
  <div class="panel">
    <div class="bar">
      <b id="edtitle">Slide</b>
      <span class="tabs"><button class="tab on" data-tab="visual">Visual</button
        ><button class="tab" data-tab="source">LaTeX</button></span>
      <span class="grow"></span>
      <button id="edprev" title="previous slide">&#9664;</button>
      <button id="ednext" title="next slide">&#9654;</button>
      <button id="ednew" title="save this slide and add a new one after it (n)">+ New</button>
      <button id="edclose">Close</button>
      <button id="edsave" class="primary">Save &amp; rebuild</button>
    </div>
    <div class="edbody">
      <div class="edmain">
        <div id="vis">
          <div class="toolbar">
            <input id="vtitle" class="titlefld" placeholder="slide title">
            <span class="sep"></span>
            <button class="t" data-ins="bold" title="bold"><b>B</b></button>
            <button class="t" data-ins="ital" title="italic"><i>I</i></button>
            <button class="t" data-ins="list" title="bullet list">&#8226;&#8801;</button>
            <button class="t" data-ins="math" title="math">$x$</button>
            <button class="t" data-ins="img" title="insert an image">image</button>
            <button class="t" data-ins="esc" title="escape % &amp; _ # in the selection">%&rarr;\%</button>
          </div>
          <div class="toolbar">
            <span class="hint">grid</span>
            <input id="grows" type="number" min="1" max="8" value="2" title="rows">
            <span class="hint">&times;</span>
            <input id="gcols" type="number" min="1" max="8" value="2" title="columns">
            <button id="gapply">Split into grid</button>
            <span class="sep"></span>
            <button id="addrow">+ row</button>
            <button id="addtext">+ full-width block</button>
            <span class="grow"></span>
            <span class="hint" id="edhint">drop an image file on any box</span>
          </div>
          <div id="parts"></div>
        </div>
        <textarea id="edtext" spellcheck="false" hidden></textarea>
      </div>
      <div class="grip" id="grip" title="drag to resize the preview &middot; double-click to reset"></div>
      <div class="edside">
        <div class="prevbox"><img id="edimg" alt="slide preview"></div>
        <div class="hint" id="edstatus"></div>
      </div>
    </div>
    <div class="bar"><span id="ederr" class="err"></span><span class="grow"></span>
      <span class="hint">&#8984;/ctrl + Enter saves &middot; Esc closes</span></div>
  </div>
</div>
<div id="picker">
  <div class="panel">
    <div class="bar"><b>Insert an image</b><span class="grow"></span>
      <span class="hint">click one, or drop a file here</span>
      <button id="pkclose">Cancel</button></div>
    <div id="pkgrid"></div>
    <div class="bar"><span id="pkmsg" class="hint"></span></div>
  </div>
</div>
<div id="ask"><div class="panel">
  <div class="askmsg" id="askmsg"></div>
  <div class="bar"><span class="grow"></span>
    <button id="askno">Cancel</button><button id="askyes" class="primary">Yes</button></div>
</div></div>
<script>
let S=null, order=[], hidden=new Set(), dirty=false, dragId=null, poll=null,
    sel=null, showLog=false, flash='', pendingRefresh=null, clickTimer=null;
const $=s=>document.querySelector(s);
const grid=$('#grid');

async function load(keep){
  const r=await fetch('api/state',{cache:'no-store'}); const st=await r.json();
  const rebuilt = !S || st.build!==S.build;
  S=st;
  // the block list can change under us (delete, insert) before the build
  // finishes - never keep an order that no longer matches it
  const ids=new Set(S.blocks.map(b=>b.id));
  const stale = order.length!==S.blocks.length || order.some(i=>!ids.has(i));
  if(rebuilt || !keep || stale){
    order=S.order.slice();
    hidden=new Set(S.blocks.filter(b=>b.disabled).map(b=>b.id));
    dirty=false;
  }
  if(sel!==null && !ids.has(sel)) sel=null;
  render();
  if(S.building && !poll){ poll=setInterval(()=>load(true),800); }
  if(!S.building && poll){ clearInterval(poll); poll=null; }
  if(!S.building && pendingRefresh!==null && edId!==null){
    const id=pendingRefresh; pendingRefresh=null; refreshPreview(id);
  }
  if(!S.building && pendingSelect!==null){
    const id=pendingSelect; pendingSelect=null;
    if(byId(id)){ sel=id; render();
      if(pendingEdit){ pendingEdit=false; edit(id); } }
    else { pendingEdit=false; }
  }
}

function byId(id){ return S.blocks.find(b=>b.id===id); }

function render(){
  document.documentElement.style.setProperty('--card',$('#zoom').value+'px');
  $('#pdflink').href='preview.pdf?'+S.stamp;
  // never trust the served HTML for these: the page itself may be a cached copy
  $('#fname').textContent=S.name;
  document.title=S.name+' — Slidewinder';
  $('#apply').disabled = !dirty || S.building;
  $('#rebuild').disabled = S.building;
  $('#newbtn').disabled = S.building;
  $('#revert').disabled = S.building || !S.backups.length;
  $('#reset').disabled = !dirty;
  const bits=[];
  if(S.building) bits.push('<span class="spin"></span> building'+(S.message?' &middot; '+S.message:'')+'\u2026');
  else bits.push(S.blocks.length+' block'+(S.blocks.length==1?'':'s')+
                 (hidden.size?' ('+hidden.size+' commented out)':'')+
                 ' &middot; '+S.npages+' page'+(S.npages==1?'':'s')+
                 (S.seconds?' &middot; built in '+S.seconds+'s':''));
  const hint=dropHint();
  if(hint) bits.push(hint);
  else if(dirty) bits.push('<b>changed \u2014 not written yet</b>');
  if(flash) bits.push('<span class="err">'+esc(flash)+'</span>');
  if(S.error) bits.push('<span class="err">'+esc(S.error)+'</span>');
  (S.warnings||[]).forEach(w=>bits.push('<span class="warn">'+esc(w)+'</span>'));
  $('#status').innerHTML=bits.join(' &nbsp;|&nbsp; ');
  const lg=$('#log'); lg.hidden=!S.log||!(showLog||S.error); lg.textContent=S.log||'';

  grid.innerHTML='';
  if(S.front && S.front.length) grid.appendChild(frontCard(S.front));
  order.forEach((id,i)=>{ const b=byId(id); if(b) grid.appendChild(card(b,i)); });
  markNeighbours();
  const im=grid.querySelector('.thumb img');
  if(im){ const set=()=>{ if(im.naturalWidth) document.documentElement.style.setProperty(
      '--ar', im.naturalWidth+'/'+im.naturalHeight); };
    im.complete? set() : im.addEventListener('load',set,{once:true}); }
}

function esc(s){ return (s+'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function frontCard(pages){
  const d=document.createElement('div');
  d.className='card locked';
  d.innerHTML='<div class="thumb">'+img(pages[0])+
    '<span class="badge">'+pages.length+'p</span></div>'+
    '<div class="meta"><span class="num">\u2014</span><span class="ttl">front matter</span></div>';
  d.onclick=()=>show(pages,'front matter');
  return d;
}

function img(p){ return p? '<img loading="lazy" src="thumb/'+S.stamp+'/'+p+'.png" alt="">' : ''; }

function card(b,i){
  const off=hidden.has(b.id);
  const d=document.createElement('div');
  d.className='card'+(b.kind!=='frame'?' section':'')+(sel===b.id?' sel':'')+(off?' off':'');
  d.draggable=true; d.dataset.id=b.id;
  let head;
  if(b.kind!=='frame'){
    head='<div class="thumb">'+esc(b.title||b.kind)+'</div>';
  }else if(b.thumb){
    head='<div class="thumb"><img loading="lazy" src="'+b.thumb+'" alt="">'+
         (off?'<span class="badge">hidden</span>'
             :(b.pages.length>1?'<span class="badge">'+b.pages.length+'p</span>':''))+'</div>';
  }else{
    head='<div class="thumb empty">'+(off?'hidden':'no preview')+'</div>';
  }
  d.innerHTML=head+'<div class="meta"><span class="num">'+(i+1)+'</span>'+
    '<span class="ttl" title="'+esc(b.title)+' (line '+b.line+')">'+
    (b.kind==='frame'? esc(b.title||'(untitled)') : '<span class="kind">'+b.kind+'</span> '+esc(b.title))+
    '</span><span class="acts">'+
    '<button class="act tog" title="'+(off?'uncomment this block (x)':'comment this block out (x)')+
    '">'+(off?'↺':'⊘')+'</button>'+
    '<button class="act ins" title="insert a new slide after this one (n)">+</button>'+
    '<button class="act del" title="delete this slide (del)">−</button>'+
    '</span></div>';
  d.querySelector('.tog').onclick=e=>{ e.stopPropagation(); toggle(b.id); };
  d.querySelector('.ins').onclick=e=>{ e.stopPropagation(); insertAfter(b.id); };
  d.querySelector('.del').onclick=e=>{ e.stopPropagation(); removeBlock(b.id); };
  if(S.building) d.querySelectorAll('.act').forEach(x=>x.disabled=true);
  d.addEventListener('dragstart',e=>{dragId=b.id;d.classList.add('drag');
    e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain',b.id);
    setTimeout(render,0);});
  d.addEventListener('dragend',()=>{dragId=null;render();});
  d.addEventListener('dragover',e=>{
    e.preventDefault();
    if(dragId===null||dragId===b.id) return;
    const from=order.indexOf(dragId), to=order.indexOf(b.id);
    const r=d.getBoundingClientRect();
    const after=(e.clientX-r.left)>r.width/2;
    let t=to+(after?1:0); if(from<t) t--;
    if(t===from) return;
    order.splice(from,1); order.splice(t,0,dragId);
    dirty=true; render();
  });
  d.addEventListener('drop',e=>e.preventDefault());
  // selection alone must not re-render: replacing the node mid-gesture loses
  // the pending timer (and can swallow the dblclick entirely)
  d.addEventListener('click',()=>{ selectOnly(b.id);
    clearTimeout(clickTimer);
    clickTimer=setTimeout(()=>{ if(!S.building) edit(b.id); },200); // let dblclick win
  });
  d.addEventListener('dblclick',()=>{ clearTimeout(clickTimer); selectOnly(b.id);
    if(b.pages.length&&!off) show(b.pages,b.title); });
  return d;
}

function selectOnly(id){
  sel=id;
  grid.querySelectorAll('.card').forEach(c=>
    c.classList.toggle('sel', c.dataset.id!==undefined && +c.dataset.id===id));
}

async function removeBlock(id){
  const b=byId(id); if(!b) return;
  if(!(await ask('Delete "'+(b.title||b.kind)+'" from the .tex? '+
                 'A backup is written first, and Revert brings it back.'))) return;
  dirty=false;
  await post('api/apply',{order:order, disabled:[...hidden], deletes:[id]});
}

/* While a card is being dragged, outline the two cards it currently sits
   between, so the landing place is never in doubt. */
function markNeighbours(){
  if(dragId===null) return;
  const i=order.indexOf(dragId);
  const el=id=>grid.querySelector('.card[data-id="'+id+'"]');
  const me=el(dragId); if(me) me.classList.add('drag');
  if(i>0){ const l=el(order[i-1]); if(l) l.classList.add('nbr','nbr-l'); }
  if(i<order.length-1){ const r=el(order[i+1]); if(r) r.classList.add('nbr','nbr-r'); }
}

function dropHint(){
  if(dragId===null) return '';
  const i=order.indexOf(dragId);
  const nm=k=>{ const b=byId(order[k]); return b? (b.title||b.kind) : null; };
  const before=i>0?nm(i-1):null, after=i<order.length-1?nm(i+1):null;
  const what=byId(dragId); if(!what) return '';
  const me=esc(what.title||what.kind);
  if(before&&after) return '<span class="drop">'+me+' → between '+esc(before)+
                            ' and '+esc(after)+'</span>';
  if(after) return '<span class="drop">'+me+' → first, before '+esc(after)+'</span>';
  if(before) return '<span class="drop">'+me+' → last, after '+esc(before)+'</span>';
  return '';
}

function toggle(id){
  if(hidden.has(id)) hidden.delete(id); else hidden.add(id);
  dirty=true; sel=id; render();
}

function move(delta){
  if(sel===null) return;
  const i=order.indexOf(sel), j=i+delta;
  if(i<0||j<0||j>=order.length) return;
  order.splice(i,1); order.splice(j,0,sel); dirty=true; render();
  const el=grid.querySelector('[data-id="'+sel+'"]'); if(el) el.scrollIntoView({block:'nearest'});
}

/* The full-size viewer.  "fit" sizes the page to the window; zooming past that
   switches to an explicit pixel width and lets the overlay scroll, so you can
   get right in on a figure. */
let zoomPct=0;                                  // 0 = fit
function show(pages,title){
  let k=0;
  const ov=$('#overlay'), im=$('#big'), cap=$('#cap');
  zoomPct=0;
  const apply=()=>{
    if(zoomPct){ im.className='zoom'; im.style.width=Math.round(
        (im.naturalWidth||1400)*zoomPct/100)+'px'; }
    else { im.className='fit'; im.style.width=''; }
  };
  const draw=()=>{ im.src='page/'+S.stamp+'/'+pages[k]+'.png'; apply();
    cap.textContent=(title||'')+'  \u2014  page '+pages[k]+
      (pages.length>1?' ('+(k+1)+'/'+pages.length+')':'')+
      (zoomPct? '  \u00b7  '+zoomPct+'%' : '')+'   [\u2190/\u2192 pages]'; };
  const zoom=d=>{
    if(!zoomPct){                               // start from what fit is showing
      zoomPct=Math.round(100*im.getBoundingClientRect().width/
                         (im.naturalWidth||1400)/25)*25 || 100;
    }
    zoomPct=Math.max(25,Math.min(400,zoomPct+d));
    draw();
  };
  const close=()=>{ ov.style.display='none'; document.onkeydown=keys; };
  ov.style.display='flex';
  im.onload=apply;
  draw();
  ov.onclick=e=>{ if(e.target===ov) close(); };  // clicks on the image do nothing
  $('#vclose').onclick=close;
  $('#zin').onclick=e=>{ e.stopPropagation(); zoom(25); };
  $('#zout').onclick=e=>{ e.stopPropagation(); zoom(-25); };
  $('#zfit').onclick=e=>{ e.stopPropagation(); zoomPct=0; draw(); };
  document.onkeydown=e=>{
    if(e.key==='Escape'){ close(); }
    else if(e.key==='ArrowRight'&&k<pages.length-1){ k++; draw(); }
    else if(e.key==='ArrowLeft'&&k>0){ k--; draw(); }
    else if(e.key==='+'||e.key==='='){ zoom(25); }
    else if(e.key==='-'||e.key==='_'){ zoom(-25); }
    else if(e.key==='0'){ zoomPct=0; draw(); }
    else return;
    e.preventDefault();
  };
  render();
}

function keys(e){
  if(edId!==null){                       // the editor owns the keyboard
    if(e.key==='Escape'){ tryClose(); e.preventDefault(); }
    return;
  }
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
  if((e.key==='x'||e.key==='X')&&sel!==null){toggle(sel);e.preventDefault();}
  else if((e.key==='e'||e.key==='E'||e.key==='Enter')&&sel!==null){edit(sel);e.preventDefault();}
  else if((e.key==='v'||e.key==='V')&&sel!==null){const b=byId(sel);
    if(b&&b.pages.length) show(b.pages,b.title); e.preventDefault();}
  else if((e.key==='n'||e.key==='N')){insertAfter(sel===null?null:sel);e.preventDefault();}
  else if((e.key==='Delete'||e.key==='Backspace')&&sel!==null){removeBlock(sel);e.preventDefault();}
  else if(e.key==='ArrowRight'&&(e.metaKey||e.ctrlKey||e.shiftKey)){move(1);e.preventDefault();}
  else if(e.key==='ArrowLeft'&&(e.metaKey||e.ctrlKey||e.shiftKey)){move(-1);e.preventDefault();}
  else if(e.key==='ArrowRight'){step(1);e.preventDefault();}
  else if(e.key==='ArrowLeft'){step(-1);e.preventDefault();}
}
function step(d){
  if(sel===null){ sel=order[0]; } else {
    const i=order.indexOf(sel); sel=order[Math.min(order.length-1,Math.max(0,i+d))];
  }
  render();
  const el=grid.querySelector('[data-id="'+sel+'"]'); if(el) el.scrollIntoView({block:'nearest'});
}
document.onkeydown=keys;

/* ---- a small promise-based confirm ------------------------------------ */
function ask(msg){
  return new Promise(res=>{
    $('#askmsg').textContent=msg; $('#ask').style.display='flex';
    const done=v=>{ $('#ask').style.display='none'; res(v); };
    $('#askyes').onclick=()=>done(true); $('#askno').onclick=()=>done(false);
  });
}

/* ---- the slide editor -------------------------------------------------- */
let edId=null, edModel=null, edKind='frame', edTab='visual', edDirty=false,
    lastBox=null, pendingSelect=null, pendingEdit=false;

function colWidth(n){ return (0.96/n).toFixed(3)+'\\textwidth'; }

/* Beamer centres a frame's content vertically, and columns[t] makes the block
   almost all depth, so a grid written with [t] sinks down the slide with a gap
   above it.  A grid belongs at the top: that needs [T] on the columns AND a
   top-aligned frame.  Anything the author already chose (c, b, s) is left be. */
const ALIGN_KEYS=['t','c','b','s'];
function colOpts(p){
  const o=(p.opts||'').trim();
  return (!o || o==='[t]') ? '[T]' : o;
}
function topOpts(opts){
  opts=opts||'';
  const m=opts.match(/^([^\[]*)\[([^\]]*)\](.*)$/);
  if(!m) return opts+'[t]';
  const keys=m[2].split(',').map(s=>s.trim()).filter(Boolean);
  if(keys.some(k=>ALIGN_KEYS.includes(k))) return opts;
  return m[1]+'['+(keys.length? keys.join(',')+',t' : 't')+']'+m[3];
}

/* model -> LaTeX.  This is the only place the frame's shape is written. */
function genLatex(m){
  const chunks=[];
  (m.parts||[]).forEach(p=>{
    if(p.kind==='text'){
      const t=(p.text||'').replace(/\s+$/,'');
      chunks.push({row:false, text:t});
    }else{
      const L=['\\begin{columns}'+colOpts(p)];
      (p.cols||[]).forEach(c=>{
        L.push('\\begin{column}{'+(c.w||colWidth(p.cols.length))+'}');
        const t=(c.text||'').replace(/\s+$/,'');
        if(t) L.push(t);
        L.push('\\end{column}');
      });
      L.push('\\end{columns}');
      chunks.push({row:true, text:L.join('\n')});
    }
  });
  const body=[];
  chunks.forEach((c,i)=>{
    if(!c.text) return;
    if(body.length) body.push(c.row&&chunks[i-1]&&chunks[i-1].row ? '\n\n\\vfill\n\n' : '\n\n');
    body.push(c.text);
  });
  const grid=(m.parts||[]).some(p=>p.kind==='row');
  const opts=grid? topOpts(m.opts||'') : (m.opts||'');
  const head='\\begin{frame}'+opts+(m.title? '{'+m.title+'}':'');
  return head+'\n'+body.join('')+'\n\\end{frame}';
}

function edText(){
  return edTab==='source' ? $('#edtext').value
                          : (edKind==='frame' ? genLatex(edModel) : $('#edtext').value);
}

/* The pane is resizable, so the preview uses the full-size render (1400px)
   rather than the grid thumbnail, which would go soft as soon as you widen it.
   A slide that is not in the PDF falls back to its cached thumbnail. */
function previewSrc(j){
  if(j.pages && j.pages.length) return 'page/'+S.stamp+'/'+j.pages[0]+'.png';
  return j.thumb || '';
}

function showPreview(j){
  const src=previewSrc(j);
  $('#edimg').src=src;
  $('#edimg').style.display=src?'block':'none';
  $('#edstatus').textContent=j.pages&&j.pages.length
      ? 'page '+j.pages.join(', ') : 'not in the PDF right now';
}

async function edit(id){
  const j=await (await fetch('api/source?id='+id,{cache:'no-store'})).json();
  if(j.error){ flash=j.error; render(); return; }
  edId=id; sel=id; edDirty=false; edKind=j.kind; lastBox=null;
  edModel=j.frame||null;
  const n=order.indexOf(id);
  $('#edtitle').textContent=(j.kind==='frame'?'Slide':j.kind)+' '+(n+1)+'/'+order.length+
    (j.disabled?'  \u2014 commented out':'');
  $('#ederr').textContent='';
  showPreview(j);
  $('#edtext').value=j.text;
  setTab(edModel? 'visual' : 'source');
  $('#editor').style.display='flex';
  const first=document.querySelector('#parts textarea.cell') ||
              (edTab==='source' ? $('#edtext') : null);
  if(first){ first.focus(); lastBox=first;
             first.setSelectionRange(first.value.length,first.value.length); }
}

function setTab(t){
  if(t==='visual'&&!edModel) t='source';
  edTab=t;
  document.querySelectorAll('.tab').forEach(b=>{
    b.classList.toggle('on',b.dataset.tab===t);
    b.disabled = (b.dataset.tab==='visual' && !edModel);
  });
  $('#vis').hidden = t!=='visual';
  $('#edtext').hidden = t==='visual';
  if(t==='visual') renderParts(); else $('#edtext').value=edText();
}

async function toSource(){
  $('#edtext').value=genLatex(edModel); edTab='source'; setTab('source');
}
async function toVisual(){
  const j=await send('api/parse',{text:$('#edtext').value});
  if(j.error){ $('#ederr').textContent=j.error; flash=''; return; }
  edModel={opts:j.opts,title:j.title,parts:j.parts}; setTab('visual');
}

function renderParts(){
  if(!edModel) return;
  $('#vtitle').value=edModel.title||'';
  const host=$('#parts'); host.innerHTML='';
  edModel.parts.forEach((p,pi)=>{
    const d=document.createElement('div'); d.className='part';
    const hdr=document.createElement('div'); hdr.className='phdr';
    hdr.innerHTML='<span>'+(p.kind==='row'? 'row of '+p.cols.length+
        (p.cols.length===1?' column':' columns') : 'full-width block')+'</span>';
    const btn=(label,title,fn)=>{ const b=document.createElement('button');
      b.textContent=label; b.title=title; b.onclick=fn; hdr.appendChild(b); return b; };
    const grow=document.createElement('span'); grow.className='grow'; hdr.appendChild(grow);
    if(p.kind==='row'){
      btn('+ col','add a column',()=>{ p.cols.push({w:'',text:''}); rewidth(p); struct(); });
      btn('\u2212 col','remove the last column',()=>{ if(p.cols.length>1){ p.cols.pop();
        rewidth(p); struct(); } });
    }
    btn('\u2191','move up',()=>{ if(pi>0){ const a=edModel.parts;
      a.splice(pi-1,0,a.splice(pi,1)[0]); struct(); } });
    btn('\u2193','move down',()=>{ const a=edModel.parts;
      if(pi<a.length-1){ a.splice(pi+1,0,a.splice(pi,1)[0]); struct(); } });
    btn('\u00d7','remove this '+(p.kind==='row'?'row':'block'),async ()=>{
      const filled=p.kind==='row'? p.cols.some(c=>(c.text||'').trim()) : (p.text||'').trim();
      if(filled && !(await ask('Remove this '+(p.kind==='row'?'row':'block')+
         ' and the text in it?'))) return;
      edModel.parts.splice(pi,1);
      if(!edModel.parts.length) edModel.parts.push({kind:'text',text:''});
      struct(); });
    d.appendChild(hdr);
    const cells=document.createElement('div'); cells.className='cells';
    if(p.kind==='row'){
      p.cols.forEach((c,ci)=>cells.appendChild(cellBox(c,'col '+(ci+1)+' \u00b7 '+
        (c.w||colWidth(p.cols.length)))));
    }else{
      cells.appendChild(cellBox(p,''));
    }
    d.appendChild(cells); host.appendChild(d);
  });
}

function cellBox(obj,label){
  const w=document.createElement('div'); w.className='cellwrap';
  if(label){ const l=document.createElement('div'); l.className='cellw';
             l.textContent=label; w.appendChild(l); }
  const ta=document.createElement('textarea');
  ta.className='cell'; ta.value=obj.text||''; ta.spellcheck=true;
  ta.oninput=()=>{ obj.text=ta.value; edDirty=true; };
  ta.onfocus=()=>{ lastBox=ta; };
  ta.addEventListener('dragover',e=>{ e.preventDefault(); ta.classList.add('drop'); });
  ta.addEventListener('dragleave',()=>ta.classList.remove('drop'));
  ta.addEventListener('drop',async e=>{
    e.preventDefault(); ta.classList.remove('drop'); lastBox=ta;
    const f=e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f) await uploadAndInsert(f);
  });
  w.appendChild(ta); return w;
}

function rewidth(p){ const w=colWidth(p.cols.length); p.cols.forEach(c=>c.w=w); }
function struct(){ edDirty=true; renderParts(); }

function makeGrid(){
  const R=Math.max(1,Math.min(8,+$('#grows').value||1));
  const C=Math.max(1,Math.min(8,+$('#gcols').value||1));
  const keep=[];
  edModel.parts.forEach(p=>{
    if(p.kind==='text'){ if((p.text||'').trim()) keep.push(p.text.trim()); }
    else p.cols.forEach(c=>{ if((c.text||'').trim()) keep.push(c.text.trim()); });
  });
  const parts=[];
  for(let r=0;r<R;r++){
    const cols=[];
    for(let c=0;c<C;c++) cols.push({w:colWidth(C), text:''});
    parts.push({kind:'row', opts:'[T]', cols:cols});
  }
  if(keep.length) parts[0].cols[0].text=keep.join('\n\n');
  edModel.parts=parts; struct();
}

/* ---- inserting text and images ---------------------------------------- */
function box(){ return lastBox || document.querySelector('#parts textarea.cell')
                || (edTab==='source' ? $('#edtext') : null); }

function putText(before,after,blockwise){
  const ta=box(); if(!ta) return;
  const s=ta.selectionStart, e=ta.selectionEnd, sel=ta.value.slice(s,e);
  let ins, caret=null;
  if(blockwise){
    const lines=sel.split('\n').map(l=>l.trim()).filter(Boolean);
    const head=(s>0 && ta.value[s-1]!=='\n') ? '\n' : '';
    if(lines.length){
      ins=head+'\\begin{itemize}\n'+lines.map(l=>'  \\item '+l).join('\n')+
          '\n\\end{itemize}\n';
    }else{
      ins=head+'\\begin{itemize}\n  \\item \n\\end{itemize}\n';
      caret=s+ins.indexOf('\\item ')+6;      // land on the empty item
    }
  }else ins=before+sel+after;
  ta.setRangeText(ins,s,e,'end');
  if(caret!==null) ta.setSelectionRange(caret,caret);
  ta.dispatchEvent(new Event('input'));
  ta.focus();
}

function escapeSpecials(){
  const ta=box(); if(!ta) return;
  const s=ta.selectionStart, e=ta.selectionEnd;
  const sel=ta.value.slice(s,e) || ta.value;
  const out=sel.replace(/(^|[^\\])([%&#_])/g,'$1\\$2');
  if(s!==e) ta.setRangeText(out,s,e,'end'); else ta.value=out;
  ta.dispatchEvent(new Event('input')); ta.focus();
}

async function openPicker(){
  const g=$('#pkgrid'); g.innerHTML='<span class="hint">looking for images\u2026</span>';
  $('#pkmsg').textContent=''; $('#picker').style.display='flex';
  const j=await (await fetch('api/images',{cache:'no-store'})).json();
  g.innerHTML='';
  if(!j.images||!j.images.length){
    g.innerHTML='<span class="hint">no image files found next to the deck \u2014 '+
                'drop one here to add it</span>';
  }
  (j.images||[]).forEach(im=>{
    const d=document.createElement('div'); d.className='pk';
    d.innerHTML='<div class="im">'+(im.shows?
      '<img loading="lazy" src="api/imgthumb?path='+encodeURIComponent(im.path)+'">':
      '<span class="hint">'+im.path.split('.').pop()+'</span>')+
      '</div><div class="nm" title="'+esc(im.path)+'">'+esc(im.path)+'</div>';
    d.onclick=()=>{ insertImage(im.path); $('#picker').style.display='none'; };
    g.appendChild(d);
  });
}

function insertImage(path){
  putText('\\includegraphics[width=\\linewidth]{'+path+'}','');
}

async function uploadAndInsert(file){
  $('#pkmsg').textContent='uploading '+file.name+'\u2026';
  const data=await new Promise(res=>{ const fr=new FileReader();
    fr.onload=()=>res(fr.result); fr.readAsDataURL(file); });
  const j=await send('api/upload',{name:file.name,data:data});
  if(j.error){ $('#ederr').textContent=j.error; $('#pkmsg').textContent=j.error;
               flash=''; return; }
  insertImage(j.path);
  $('#pkmsg').textContent='saved as '+j.path;
  $('#picker').style.display='none';
}

/* ---- saving ------------------------------------------------------------ */
function closeEditor(){ $('#editor').style.display='none'; edId=null; edModel=null; }

async function tryClose(){
  if(edDirty && !(await ask('Close without saving your changes?'))) return;
  closeEditor(); await load(true);
}

async function saveEdit(keepOpen){
  if(edId===null) return;
  if(S.building){ $('#ederr').textContent='a build is running \u2014 one moment'; return; }
  const body={order:order, disabled:[...hidden], edits:{}};
  body.edits[edId]=edText();
  const j=await send('api/apply',body);
  if(j.error){ $('#ederr').textContent=j.error; flash=''; return; }
  $('#ederr').textContent=''; edDirty=false; dirty=false;
  const id=edId;
  await load(true);
  if(keepOpen===false){ closeEditor(); return; }
  pendingRefresh=id;                        // refresh the preview after the build
}

/* New slide from inside the editor: whatever you have typed is saved and the
   slide is added in the same write, so it costs one backup and one build and
   the editor moves to the new slide when it comes back. */
async function edNew(){
  if(edId===null || S.building) return;
  const t=await fetch('api/template',{cache:'no-store'});
  const tpl=(await t.json()).text;
  const body={order:order, disabled:[...hidden], edits:{},
              inserts:[{after:edId, text:tpl}]};
  if(edDirty) body.edits[edId]=edText();
  const j=await send('api/apply',body);
  if(j.error){ $('#ederr').textContent=j.error; flash=''; return; }
  $('#ederr').textContent=''; edDirty=false; dirty=false;
  if(j.new && j.new.length){ pendingSelect=j.new[0]; pendingEdit=true; }
  await load(true);
}

async function edStep(d){
  const i=order.indexOf(edId);
  if(i<0) return;
  const j=i+d; if(j<0||j>=order.length) return;
  if(edDirty && !(await ask('Move to the next slide without saving?'))) return;
  edDirty=false; await edit(order[j]);
}

async function refreshPreview(id){
  const j=await (await fetch('api/source?id='+id,{cache:'no-store'})).json();
  if(j.error) return;
  showPreview(j);
  if(edTab==='visual'&&j.frame){ edModel=j.frame; renderParts(); }
  else if(edTab==='source') $('#edtext').value=j.text;
}

async function insertAfter(id){
  if(S.building) return;
  const t=await fetch('api/template',{cache:'no-store'});
  const tpl=(await t.json()).text;
  const j=await send('api/apply',{order:order, disabled:[...hidden],
                                  inserts:[{after:id, text:tpl}]});
  // only now do we know where the new block landed - the server says so
  if(j && !j.error && j.new && j.new.length){
    pendingSelect=j.new[0]; pendingEdit=true; dirty=false;
  }
  await load(true);
}

async function send(url,body){
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(body||{})});
    const j=await r.json();
    flash=j.error||'';
    return j;
  }catch(err){ flash=''+err; return {error:''+err}; }
}

async function post(url,body){ await send(url,body); await load(true); }
$('#apply').onclick=()=>{ dirty=false;
  post('api/apply',{order:order, disabled:[...hidden]}); };
$('#rebuild').onclick=()=>post('api/rebuild',{});
$('#reset').onclick=()=>{ order=S.order.slice();
  hidden=new Set(S.blocks.filter(b=>b.disabled).map(b=>b.id)); dirty=false; render(); };
$('#revert').onclick=async ()=>{ if(await ask('Restore the most recent backup and rebuild?'))
  post('api/revert',{}); };
$('#newbtn').onclick=()=>insertAfter(sel===null?null:sel);
$('#edclose').onclick=tryClose;
$('#edsave').onclick=()=>saveEdit(true);
$('#edprev').onclick=()=>edStep(-1);
$('#ednext').onclick=()=>edStep(1);
$('#ednew').onclick=edNew;
$('#vtitle').oninput=e=>{ if(edModel){ edModel.title=e.target.value; edDirty=true; } };
$('#gapply').onclick=makeGrid;
$('#addrow').onclick=()=>{ const C=Math.max(1,Math.min(8,+$('#gcols').value||2));
  const cols=[]; for(let i=0;i<C;i++) cols.push({w:colWidth(C),text:''});
  edModel.parts.push({kind:'row',opts:'[T]',cols:cols}); struct(); };
$('#addtext').onclick=()=>{ edModel.parts.push({kind:'text',text:''}); struct(); };
$('#pkclose').onclick=()=>{ $('#picker').style.display='none'; };
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  if(b.dataset.tab===edTab) return;
  if(b.dataset.tab==='source') toSource(); else toVisual();
});
document.querySelectorAll('.toolbar .t').forEach(b=>b.onclick=()=>{
  const k=b.dataset.ins;
  if(k==='bold') putText('\\textbf{','}');
  else if(k==='ital') putText('\\emph{','}');
  else if(k==='math') putText('$','$');
  else if(k==='list') putText('','',true);
  else if(k==='esc') escapeSpecials();
  else if(k==='img') openPicker();
});
['#pkgrid'].forEach(s=>{
  const el=$(s);
  el.addEventListener('dragover',e=>{ e.preventDefault(); el.classList.add('drop'); });
  el.addEventListener('dragleave',()=>el.classList.remove('drop'));
  el.addEventListener('drop',async e=>{ e.preventDefault(); el.classList.remove('drop');
    const f=e.dataTransfer.files&&e.dataTransfer.files[0]; if(f) await uploadAndInsert(f); });
});
$('#edtext').addEventListener('input',()=>{ edDirty=true; });
$('#edtext').addEventListener('focus',()=>{ lastBox=$('#edtext'); });
$('#editor').addEventListener('keydown',e=>{
  if(e.key==='Escape'){ if($('#picker').style.display==='flex'){
      $('#picker').style.display='none'; } else tryClose(); e.preventDefault(); }
  else if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){ saveEdit(true); e.preventDefault(); }
  else if(e.key==='Tab'&&e.target.tagName==='TEXTAREA'){ const t=e.target, s=t.selectionStart;
    t.setRangeText('  ',s,t.selectionEnd,'end'); t.dispatchEvent(new Event('input'));
    e.preventDefault(); }
});
$('#editor').addEventListener('dragover',e=>{ if(e.dataTransfer.types.includes('Files'))
  e.preventDefault(); });
$('#editor').addEventListener('drop',e=>{ if(e.dataTransfer.files.length) e.preventDefault(); });
/* ---- resizable preview pane ------------------------------------------- */
const SIDE_DEFAULT=340, SIDE_MIN=220;
function setSide(px){
  document.documentElement.style.setProperty('--side', Math.round(px)+'px');
}
try{ const w=parseInt(localStorage.getItem('sw.side')||'',10);
     if(w>=SIDE_MIN) setSide(w); }catch(err){}
(function(){
  const grip=$('#grip');
  let panel=null;
  const move=ev=>{
    if(!panel) return;
    const max=Math.max(SIDE_MIN, panel.width-380);
    setSide(Math.max(SIDE_MIN, Math.min(max, panel.right-ev.clientX)));
  };
  const up=ev=>{
    panel=null; grip.classList.remove('on');
    window.removeEventListener('pointermove',move);
    window.removeEventListener('pointerup',up);
    try{ localStorage.setItem('sw.side',
        parseInt(getComputedStyle($('#editor .edside')).width,10)); }catch(err){}
  };
  grip.addEventListener('pointerdown',ev=>{
    ev.preventDefault();
    panel=$('#editor .panel').getBoundingClientRect();
    grip.classList.add('on');
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',up);
  });
  grip.addEventListener('dblclick',()=>{ setSide(SIDE_DEFAULT);
    try{ localStorage.setItem('sw.side',SIDE_DEFAULT); }catch(err){} });
})();

$('#logbtn').onclick=()=>{ showLog=!showLog; render(); };
$('#zoom').oninput=render;
grid.addEventListener('dragover',e=>e.preventDefault());
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    project: Project = None
    server_version = "slidewinder/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("SLIDEWINDER_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # Only successfully served images may be cached, and their URLs carry a
        # per-run stamp.  Everything else - HTML, JSON, the PDF, and every error
        # - must not be, or a later run of a different deck inherits it.
        cacheable = code == 200 and ctype.startswith("image/")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "max-age=86400" if cacheable else "no-store, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _file(self, path: Path, code=200):
        if path is None or not Path(path).exists():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(code, Path(path).read_bytes(), ctype)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:                                  # noqa: BLE001
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        p = urlparse(self.path).path
        pr = self.project
        if p in ("/", "/index.html"):
            return self._send(200, PAGE.replace("__NAME__", pr.tex.name), "text/html; charset=utf-8")
        m = re.fullmatch(r"/thumb/[\w.-]+/(\d+)\.png", p)
        if m:
            return self._file(pr.thumb(int(m.group(1))))
        m = re.fullmatch(r"/page/[\w.-]+/(\d+)\.png", p)
        if m:
            return self._file(pr.big_page(int(m.group(1))))
        m = re.fullmatch(r"/cache/[\w.-]+/([0-9a-f]{4,40})\.png", p)
        if m:
            return self._file(pr.cache / (m.group(1) + ".png"))
        if p == "/api/state":
            return self._json(pr.state())
        if p == "/api/source":
            q = parse_qs(urlparse(self.path).query)
            try:
                return self._json(pr.block_source(int(q.get("id", ["-1"])[0])))
            except Exception as exc:                        # noqa: BLE001
                return self._json({"error": str(exc)})
        if p == "/api/template":
            return self._json({"text": pr.new_frame()})
        if p == "/api/images":
            try:
                return self._json({"images": pr.images()})
            except Exception as exc:                        # noqa: BLE001
                return self._json({"error": str(exc), "images": []})
        if p == "/api/imgthumb":
            q = parse_qs(urlparse(self.path).query)
            try:
                return self._file(pr.image_thumb(q.get("path", [""])[0]))
            except Exception:                               # noqa: BLE001
                return self._send(404, b"no thumbnail", "text/plain")
        if p == "/preview.pdf":
            return self._file(pr.build / "preview.pdf")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        pr = self.project
        body = self._body()
        try:
            if p == "/api/rebuild":
                pr.rebuild_async()
                return self._json({"ok": True})
            if p == "/api/apply":
                order = [int(x) for x in body.get("order", [])]
                disabled = [int(x) for x in body.get("disabled", [])]
                if pr.building:
                    return self._json({"error": "a build is already running"})
                backup, new_ids = pr.apply_order(order, disabled,
                                                 body.get("edits"),
                                                 body.get("inserts"),
                                                 body.get("deletes"))
                pr.rebuild_async("wrote %s" % (backup.name if backup else "no change"))
                return self._json({"ok": True, "new": new_ids,
                                   "backup": backup.name if backup else None})
            if p == "/api/parse":
                fr = parse_frame(body.get("text") or "")
                if not fr:
                    return self._json({"error": "that is not a frame"})
                return self._json(fr)
            if p == "/api/upload":
                import base64
                raw = base64.b64decode((body.get("data") or "").split(",")[-1])
                if len(raw) > 25 * 1024 * 1024:
                    return self._json({"error": "that file is over 25MB"})
                return self._json({"path": pr.save_upload(body.get("name"), raw)})
            if p == "/api/revert":
                if pr.building:
                    return self._json({"error": "a build is already running"})
                src = pr.revert(body.get("name"))
                pr.rebuild_async("restored %s" % src.name)
                return self._json({"ok": True, "restored": src.name})
        except Exception as exc:                            # noqa: BLE001
            return self._json({"error": str(exc)})
        return self._json({"error": "unknown endpoint"}, 404)


# --------------------------------------------------------------------------

EXTRA_PATHS = [
    "/Library/TeX/texbin",          # MacTeX
    "/usr/local/texlive/2025/bin/universal-darwin",
    "/opt/homebrew/bin",            # Apple silicon homebrew
    "/usr/local/bin",               # intel homebrew
    "/opt/local/bin",               # macports
    os.path.expanduser("~/bin"),
    os.path.expanduser("~/Library/TinyTeX/bin/universal-darwin"),
]


def augment_path():
    """GUI shells and bare login shells often miss MacTeX / homebrew."""
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for d in EXTRA_PATHS:
        if os.path.isdir(d) and d not in parts:
            parts.append(d)
    os.environ["PATH"] = os.pathsep.join(parts)


def check_deps(engine="xelatex", verbose=True):
    """Report on the two external needs.  Returns True if we can run."""
    eng = shutil.which(engine)
    rend = renderer_name()
    if verbose:
        print("python      %s" % sys.executable)
        print("in venv     %s" % ("yes" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "no"))
        print("%-11s %s" % (engine, eng or "NOT FOUND"))
        print("renderer    %s" % (rend or "NOT FOUND"))
    ok = True
    if not eng:
        ok = False
        if verbose:
            print("""
  %s is not on PATH.  It cannot come from pip - it is a TeX distribution.
    macOS:  brew install --cask mactex-no-gui      (or install MacTeX)
            then open a new shell, or add /Library/TeX/texbin to PATH
    If TeX is already installed, find it with:  ls /Library/TeX/texbin/xelatex
    You can also point at another engine:  python3 slidewinder.py talk.tex --engine pdflatex
""" % engine)
    if not rend:
        ok = False
        if verbose:
            print("""
  No way to turn PDF pages into PNGs.  Either of these fixes it:
    ./setup.sh                      creates .venv with pypdfium2 (pure pip, no system deps)
    brew install poppler            installs pdftoppm system-wide
""")
    if verbose and ok:
        print("\nall set - run:  python3 slidewinder.py yourtalk.tex")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(prog="slidewinder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tex", nargs="?", help="the beamer .tex file")
    ap.add_argument("--check", action="store_true",
                    help="report on xelatex / PDF renderer availability and exit")
    ap.add_argument("-p", "--port", type=int, default=8737)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--engine", default="xelatex",
                    help="latex engine (default: xelatex)")
    ap.add_argument("--passes", type=int, default=2,
                    help="latex passes per build (default: 2)")
    ap.add_argument("--width", type=int, default=640,
                    help="thumbnail render width in px (default: 640)")
    ap.add_argument("--renderer", choices=("auto", "pdftoppm", "pypdfium2"),
                    default="auto",
                    help="force a PDF renderer (default: pdftoppm if installed)")
    ap.add_argument("--no-sections", action="store_true",
                    help="do not treat \\section commands as movable")
    ap.add_argument("--subsections", action="store_true",
                    help="also treat \\subsection commands as movable")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the page map and HTTP requests")
    args = ap.parse_args(argv)
    augment_path()
    global FORCE_RENDERER
    FORCE_RENDERER = None if args.renderer == "auto" else args.renderer

    if args.check:
        sys.exit(0 if check_deps(args.engine) else 1)
    if not args.tex:
        ap.error("a .tex file is required (or use --check)")

    tex = Path(args.tex).expanduser()
    if not tex.exists():
        sys.exit("no such file: %s" % tex)
    if not check_deps(args.engine, verbose=False):
        print("slidewinder: missing dependencies\n")
        check_deps(args.engine)
        sys.exit(1)

    pr = Project(tex, engine=args.engine, passes=args.passes, dpi=args.width,
                 sections=not args.no_sections, subsections=args.subsections)
    pr.verbose = args.verbose
    if args.verbose:
        os.environ["SLIDEWINDER_VERBOSE"] = "1"
    Handler.project = pr

    print("slidewinder %s  %s" % (VERSION, pr.tex))
    print("engine %s, renderer %s" % (args.engine, renderer_name()))
    print("building ...", flush=True)
    pr.building = True
    try:
        pr.rebuild()
    finally:
        pr.building = False
    if pr.error:
        print("  ! %s" % pr.error)
    print("  %d blocks, %d pages, %.1fs" % (len(pr.doc.blocks), pr.npages, pr.last_build_s))

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    # the query makes this a fresh URL every run, so a page cached by an earlier
    # run (or an older version of this app) can never be served instead
    url = "http://%s:%d/?r=%s" % (args.host, args.port, pr.run_id)
    print("serving %s   (Ctrl-C to stop)" % url)
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
