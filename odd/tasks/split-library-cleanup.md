# Split library cleanup

## Objective
Simplify the Split library view: drop the unused filter popover, make the row detail column readable, and restore file drag-and-drop once a catalog exists.

## Problem / why
- The filter icon opens a popover with "All profiles" / "All statuses" dropdowns that nobody uses; it competes with search and sort for attention.
- The third row column packs six values into one 9px label (`profile · duration · BPM · Key · genre · date`). `bpm` and `musical_key` are never populated (`history.py` creates them as `None` and nothing updates them), so the column always shows `BPM — · Key —`. Genre rarely exists in tags.
- `_register_drop_zone` only covers the empty-state drop zone. When the library panel replaces it, no widget is a `DND_FILES` target, so dropping a file does nothing; only the "Add song" button works.

## Scope (authorized)
- `SeparationWorker/gui.py`: `_build_library_view`, `_toggle_library_filters`, `_library_query`, `_render_library`, `_register_drop_zone`.
- Portable tests covering the new row detail text.
- README wording if it mentions library filters.

## Out of scope
- Computing BPM/key; changing the history schema; redesigning the empty state; any Mixer/Export change.

## Constraints
- English UI copy. Conventional Commits, no AI attribution lines.
- TDD: off (no project/session configuration found; frameworks present do not enable it). Runner: `.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v`.
- RDD: off (decided by default). Delivery strategy: `ask-on-risk`. Forecast ~120 authored changed lines.
- Branch: `feat/split-library-cleanup` from `master`.

## Tasks
- [x] T1 — Remove the library filter button and popover. Delete `_toggle_library_filters`, `_library_filter_button`, `_library_filter_popover`, `library_profile_filter`, `library_status_filter`; `_library_query` sends `profile_id="all"`, `status="all"`. Keep search, sort and the "No songs match" message (search-only wording).
  - Route: inline (one file, mechanical). Checks: Portable suite green; GUI row tests still pass.
- [x] T2 — Readable detail column. Add a headless `library_row_detail(record, *, profile_name, show_profile)` helper returning `duration · date`, prefixed by the profile name only when the catalog holds more than one distinct `profile_id`. Drop BPM/Key/genre. Add portable unit tests for the helper.
  - Route: inline (gui.py + one small test file). Checks: new tests RED→GREEN observed; Portable suite green.
- [x] T3 — Restore drag-and-drop on the library. Register the library panel as a `DND_FILES` target at build time and re-register newly rendered rows after each `_render_library`; guard `_register_drop_zone` against double registration. Drops reuse `_drop_input` → `_choose_profile`. Update the library subtitle to say files can be dropped.
  - Route: inline (one file). Checks: Portable suite green; manual drop of an audio file on a populated library opens the profile dialog.

- [x] T4 — Library layout on wide windows. Cap the library content to a centered max width (~1100px, same principle as the 600px empty state); render each row as a grid (dot · title stretches · artist · detail · open · remove) so actions sit next to their row and columns stay aligned regardless of title length; raise type scale (title 12→14, artist/detail 10→12) with row padding 14→12.
  - Route: inline (one file + existing real-widget tests). Checks: Portable suite green; app launched and window captured at a wide size.

## Acceptance criteria
- No filter icon or popover in the library toolbar; search and sort still work.
- Row detail shows `MM:SS · YYYY-MM-DD` (profile prefix only with mixed profiles); never shows `BPM —` / `Key —`.
- Dropping an audio file anywhere on the populated library view opens the same profile dialog as "Add song".
- On a 2000px-wide window the row actions sit within the capped content width, artist/detail columns align across rows, and text is readable at 12–14pt.

## Progress / evidence
- T1 — commit `24ae933`. Route inline (one file, mechanical; also removed the orphaned `sliders` icon). `python -m unittest discover -s Tests/Portable`: 389 tests OK. RDD off → assessed n/a (unmanaged).
- T2 — commit `eaa2752`. Route inline (gui.py + test_gui_view_state.py). New `LibraryRowDetailTests` observed RED (ImportError) then GREEN; suite 392 OK. Detail font 9 → 10 since the column now holds two values.
- T3 — commit `02641bc`. Route inline (gui.py + test_gui_library_rows.py). New `LibraryDropTargetTests` observed RED (2 failures with gui.py stashed) then GREEN; suite 395 OK. `tkdnd::drop_target gettypes` returned empty even on mapped widgets, so the tests assert the `<<Drop>>` binding via `tk.call("bind", w, ...)` (CTk redirects `.bind()` to its inner canvas).
- Manual: launched the app against the real library (5 tracks) and captured the window — no filter icon, detail reads `06:03 · 2026-09-16`, subtitle mentions dropping files. A real Explorer drag-and-drop was NOT exercised by the agent; pending user check.
- T4 — commit `856c37a`. Route inline (one file; real-widget tests unchanged and green, incl. the long-title button-width regression). Suite 395 OK. Captured at 2000x800 and 900x700: content capped/centered, columns aligned, actions next to their row. User confirmed the real Explorer drop on 2026-09-19 (new track appeared).
- Authored changed lines: ~260 (under the 400 delivery budget → single PR).

## Next step
Push `feat/split-library-cleanup` and open a single PR (user decision).
