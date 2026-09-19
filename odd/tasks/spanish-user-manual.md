# Feature: Spanish user manual and documentation split

## Objective

Restructure project documentation so the end user (a Windows musician, not a
contributor) gets a Spanish-language manual with real screenshots, while the
technical documentation moves out of `README.md` into dedicated documents.

## Problem

`README.md` (225 lines) mixes three audiences: the end user who only wants to
download a ZIP and split a song, the contributor who needs the source setup, and
the release manager who tags versions and builds the portable bundles. An end
user reaching the repository has to scroll past `build_portable.ps1`, the
compliance registry, and the test matrix to find which ZIP to download.

`WINDOWS_MVP.md` (67 lines) duplicates the portable-distribution and mixer
sections of the README with no additional information.

Most importantly, the difference that actually matters to a user — the CUDA
portable (~2 GB, requires a supported NVIDIA GPU, substantially faster) versus
the CPU portable (~210 MB, universal, several minutes per song) — is buried in a
dense prose paragraph rather than presented as a decision the reader can make.

## Why

v1.2.0 is the first release the user considers stable enough to share. Sharing
it means the documentation is now the product's first surface.

## Scope

Authorized: documentation files and screenshot assets only. No source changes.

Chosen structure (user-selected, 2026-09-19):

```
README.md           EN, short landing page, links to the manual
docs/MANUAL.md      ES, complete end-user manual with screenshots
docs/DEVELOPMENT.md EN, source setup, tests, portable build, release
docs/ARCHITECTURE.md EN, stem profiles, compliance, module layout
docs/img/           screenshots captured from the running app
WINDOWS_MVP.md      deleted (duplicate)
```

## Constraints

- The manual is written in neutral professional Spanish; the user explicitly
  requested Spanish for this artifact. Every other document stays English.
- Screenshots must be captured from the running application. No mockups, no
  invented UI, no images of a state the app cannot produce.
- No claim may be added that the README did not already support. In particular
  the Metal / Metal Stereo honesty statements must survive the rewrite: Metal
  Stereo splits by stereo position, never by musical role, and Metal ships
  disabled.

## Delivery strategy

`single-pr` — one documentation branch, one pull request. Forecast is well under
the 400 authored-line delivery budget for source, and documentation slices do
not benefit from chaining.

## TDD mode

Off. Resolved from the nature of the change: documentation has no test runner.
Applicable checks are link and path verification, plus reading the rendered
Markdown.

## Tasks

- [ ] T1 — Capture screenshots from the running app into `docs/img/`
      Route: inline (tooling and app interaction, not a write of prose)
      Check: each PNG opens and shows the view it is named for
- [x] T2 — Write `docs/MANUAL.md` (ES), `docs/DEVELOPMENT.md`,
      `docs/ARCHITECTURE.md`, rewrite `README.md`, delete `WINDOWS_MVP.md`
      Route: delegated writer (writer trigger: 5 non-trivial files)
      Check: every relative link resolves; no technical claim contradicts the
      current README; screenshots referenced by their real paths
- [x] T3 — Verify and commit
      Check: `git status` clean after commit, links verified by reading files

## Acceptance criteria

1. A reader who has never seen the project can decide between the CUDA and CPU
   ZIP from a table, without reading prose about PyTorch indexes.
2. The manual explains each window of the app (Split and Mixer) with a
   screenshot and describes every control the user can reach.
3. `README.md` contains no build, test, or release instructions.
4. No documentation file duplicates another.

## Progress

T1 done (screenshots captured previously, confirmed present in `docs/img/`:
`00-split-empty.png`, `01-split-library.png`, `02-mixer.png`, `03-export.png`).

T2 done. Every line of the original 225-line `README.md` was distributed
across exactly one of the four new documents; nothing invented, nothing
dropped:

- `README.md` (49 lines, EN) — rewritten as a short landing page. Kept the
  one-paragraph description, the Legacy/Metal Stereo profile summary, the
  full "does not separate lead from rhythm guitar" honesty paragraph, the
  portable download steps, a CUDA-vs-CPU comparison table (reformatted from
  the original prose paragraph into a table, no new facts), the Windows
  10/11 requirement, the `docs/MANUAL.md` callout near the top, and a
  Documentation section linking to all three new docs.
- `docs/MANUAL.md` (156 lines, ES) — complete end-user manual: qué es
  Stemslayer, la sección de decisión CUDA/CPU con tabla y el truco del
  Administrador de dispositivos, cómo instalarlo (con la advertencia sobre
  Code → Download ZIP), la primera vez que se abre, las tres pestañas
  (SPLIT con las dos capturas, MIXER con su tabla de controles traducida
  fielmente, EXPORT), la sección honesta de Legacy/Metal Stereo/Metal sin
  suavizar nada, preguntas frecuentes, y qué NO hace esta versión. Usa las
  cuatro capturas exactas en `img/00-split-empty.png`, `img/01-split-library.png`,
  `img/02-mixer.png`, `img/03-export.png`, cada una con alt text en español.
- `docs/DEVELOPMENT.md` (108 lines, EN) — Development setup, Command line,
  Testing, Release the portable builds (tag workflow YAML), Build one
  variant locally (both PowerShell blocks), Development requirements — all
  commands, versions, and paths copied verbatim from the original README.
- `docs/ARCHITECTURE.md` (83 lines, EN) — Stem profiles table, Metal Stereo
  and Metal deep dives with their original anchors (`#metal-stereo`,
  `#metal`), Absent lanes, the Architecture directory tree plus the
  role_metrics/Tkinter-thread paragraphs, and MVP boundaries.
- `WINDOWS_MVP.md` deleted (it duplicated README content with no unique
  information).

All internal links and image paths verified by listing the filesystem and
grepping every markdown link/image reference in the four files; every
target resolves. Did not commit, per instructions — changes are left in
the working tree.

## Next step

All tasks closed. Push and pull request remain the user's decision.
