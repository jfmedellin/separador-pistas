# Global type scale

## Objective
Apply one readable type scale to every Stemslayer view so the Split empty state, the Mixer, the transport and both dialogs read at the same size as the redesigned split library.

## Problem / why
After the library moved to 12/14/22pt, the rest of the app still renders 9–11pt text: Split empty state mostly 10pt, Mixer lane labels 11pt (9pt when absent), lane toggles and percent readouts 9pt, transport status 10pt, profile dialog 9–11pt, export dialog 10–11pt. On a 1080p+ desktop that reads as fine print, and the inconsistency makes the library look like a different product.

## Scope (authorized)
- `SeparationWorker/gui.py`: replace every literal `("Segoe UI", n[, "bold"])` / `("Consolas", n)` with one module-level scale so the size lives in one place.
- Portable tests that assert the scale exists and that no literal sizes below the caption floor remain.
- Screenshots of every view after the change.

## Out of scope
- Layout or copy changes beyond what a larger font forces; colours; icons; the library (already done in `feat/split-library-cleanup`).

## Constraints
- English UI copy. Conventional Commits, no AI attribution lines.
- TDD: off (no project config). Runner: `.venv\Scripts\python.exe -m unittest discover -s Tests\Portable -v`.
- RDD: off (decided by default). Delivery: `ask-on-risk`; forecast ~120 authored changed lines → single PR.
- Branch `feat/global-type-scale`, based on `feat/split-library-cleanup` (PR #17) so it merges cleanly after that PR lands.
- `mixer_chrome_height()` is measured live from the widgets, so header/transport growth is absorbed. `LANE_CONTENT_HEIGHT = 96` is fixed: the lane head row (22px toggles + label) + 8px + 14px slider must still fit, so verify with the six-lane profile.

## The scale
| Role | Size | Used for |
|---|---|---|
| `label` | 11 bold | uppercase section labels (INPUT AUDIO, PROFILE, CHANNELS TO EXTRACT), nav tabs stay 12 bold |
| `caption` | 12 | secondary/status text, notes, file names, readouts (mono 11) |
| `body` | 12 | buttons, checkboxes, entries, option menus, chips |
| `title` | 14 bold | row titles, lane names, dialog row titles |
| `heading` | 18 bold | mixer title, dialog headings (profile 17→18, export 16→18) |
| `display` | 22 bold | view headers (hero headline, library header) |

Nothing renders below 11pt. Absent lanes keep the muted colour, not a smaller size.

## Tasks
- [x] T1 — Introduce the scale in `gui.py`: `FONT_FAMILY`, `MONO_FAMILY`, `TYPE_SCALE` and a `ui_font(role, *, bold=False, mono=False)` helper; add a portable test that every role resolves and that no literal `("Segoe UI", n)` / `("Consolas", n)` tuple remains in `gui.py` (regex scan of the source).
  - Route: delegated writer (one file + one test; ~40 font sites). Checks: new test RED then GREEN; Portable suite green.
- [x] T2 — Apply the scale to every view per the table above (nav, Split empty state, library — only where it deviates, Mixer header/lanes/toggles/percent, transport, profile dialog, export dialog, status banners). Grow fixed widths only where the larger font would clip (`percent` label width, lane controls frame, dialog geometry).
  - Route: same writer as T1 (single commit is fine if the scan test is what proves T1). Checks: Portable suite green; screenshots of Split empty state, library, Mixer with a loaded 4-lane session, profile dialog, export dialog, at 1200x800.

## Acceptance criteria
- No text in any view renders below 11pt; captions/body at 12, titles 14, headings 18, display 22.
- Mixer with the six-lane profile shows every lane control unclipped at the default window height.
- Profile and export dialogs show all content without clipping.
- `python -m unittest discover -s Tests/Portable` green.

## Progress / evidence
- T1 + T2 — commit `0658853` (one writer, delegated; parent spot-checked `TypeScaleTests` and re-ran the suite). Scan test observed RED with 44 literal font sites, GREEN after the rewrite. `python -m unittest discover -s Tests/Portable`: 397 OK.
- Captures at 1200x800: Split empty state, profile dialog, library, Mixer with a loaded 4-lane session, export overlay. One defect found in the first round — the profile dialog's fixed `480x400` clipped the third row — fixed by sizing the dialog to `body.winfo_reqheight()`; confirmed in a second capture.
- RDD off → unmanaged. Authored changed lines: 93+/49−.

## Next step
Push `feat/global-type-scale` and open a PR against `master` once PR #17 is merged (user decision).
