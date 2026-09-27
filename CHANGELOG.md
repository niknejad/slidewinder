# Changelog

## 1.0.4 — 2026-09-27

- **+ New** in the editor: adds a slide after the current one and moves the
  editor to it. Unsaved work on the current slide goes out in the same write,
  so it is one backup and one build rather than two.
- The full-size viewer fills the window and zooms: **− / fit / +** buttons and
  the `-`, `0`, `+` keys, 25% to 400%, scrolling when zoomed past the window.
- Fixed double-click on a card opening the editor on top of the viewer.
  Selecting a card used to re-render the whole grid, which replaced the node
  mid-gesture and stranded the pending single-click timer; selection now just
  moves a class.

## 1.0.3 — 2026-09-27

- The editor's preview pane is resizable: drag the divider, double-click it to
  reset, and the width is remembered between sessions. It is clamped so the
  editor keeps at least 380px.
- The preview now uses the full-size page render rather than the grid
  thumbnail, so it stays sharp at any width instead of going soft as soon as
  the pane is widened.

## 1.0.2 — 2026-09-27

- **Grids now sit at the top of the slide.** They were written with
  `\begin{columns}[t]`, which aligns baselines and leaves the block almost all
  depth; beamer then centres it and you get a band of blank space above the
  grid. New grids use `[T]` on the columns and add `t` to the frame's options,
  which is what actually top-aligns both the block and the cells. A frame that
  already asks for `c`, `b` or `s` keeps it. Existing grids are corrected the
  next time you save that slide from the Visual tab.

## 1.0.1 — 2026-09-27

- **Fixed a crash that killed the server** (`malloc: pointer being freed was not
  allocated`, or a segfault) when opening the image picker on a deck whose
  figures are PDFs. PDFium is not thread-safe and the server is threaded, so the
  browser's parallel thumbnail requests were rendering concurrently and
  corrupting the heap. All pypdfium2 use is now serialised behind one lock.
  Only decks using the pypdfium2 renderer — that is, machines without poppler —
  were affected.
- Fixed parallel renders deleting each other's output files, which made
  thumbnails intermittently come back 404.
- Added `--renderer {auto,pdftoppm,pypdfium2}` to force one renderer.

## 1.0.0 — 2026-09-27

First public release, under the name **Slidewinder** (previously `beamer_sort`).
An existing `.beamer_sort/` work folder is moved to `.slidewinder/` on first
run, so old backups stay reachable.

### Sorter
- One draggable card per `\begin{frame}` block, old-style `\frame{...}`, and
  `\section` / `\subsection`.
- Overlay pages (`\pause`, `<1->`) group under their frame with a page-count
  badge; double-click pages through them full size.
- Drag feedback: the dragged card goes dashed, the two cards it will land
  between are outlined on the facing edges, and the status line names them.
- Apply writes the reordered `.tex`, backs it up, recompiles and redisplays.

### Editor
- Click a slide to open it, with the rendered page beside the text.
- Visual tab: a title field and one box per region, with a toolbar for bold,
  italic, lists, math, images and escaping `% & # _`.
- LaTeX tab: the whole frame's source; switching back re-parses it.
- Split a slide into *rows × columns* of `columns` environments, with per-row
  add/remove column, reorder and delete. Existing content lands in the first
  cell. Frames already built from `columns` are parsed back into the same grid.
- Image picker over the files near the deck, plus drag-and-drop upload into
  `figures/`.
- Insert (`+`, `n`) and delete (`−`, Delete) slides.

### Rest
- Comment a slide out instead of deleting it; already-commented blocks are
  detected, shown dimmed in place, and restored byte-identically.
- Exact page→frame mapping via a `\bsblk` marker and an `\AtBeginShipout` hook,
  with a warning when the map and the PDF disagree.
- Renders with poppler's `pdftoppm`, or `pypdfium2` from pip when poppler is
  absent; `--check` reports what it found.
- Every write is backed up; **Revert** restores the newest backup.
