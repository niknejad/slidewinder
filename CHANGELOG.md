# Changelog

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
