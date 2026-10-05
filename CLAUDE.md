# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bambu Lab to Snapmaker U1 Converter - A Flask web application that converts Bambu Lab/Bambu Studio .3mf files to Snapmaker U1 compatible format, preserving multi-color painting and filament assignments.

## Commands

```bash
# Install dependencies
pip install flask

# Run the application (starts on port 8080)
python app.py

# The app will be available at http://localhost:8080
```

## Architecture

### Single-File Flask Backend (`app.py`)
- **Single File Routes**: `/` (UI), `/analyze` (POST), `/convert` (POST), `/download/<filename>` (serves files), `/filament-types` (returns filament profiles)
- **Batch Routes**: `/batch-analyze` (POST - analyzes multiple files), `/batch-convert` (POST - converts all to output folder)
- **Settings/History Routes**: `/settings` (GET/POST), `/history` (GET), `/history/clear` (POST), `/browse` (GET), `/check-new` (GET), `/convert-new` (POST)
- **File handling**: Uploads stored in `uploads/` with UUID-based session IDs, auto-cleaned after 8 hours
- **3MF Processing**: Uses zipfile + xml.etree.ElementTree to modify internal XML/JSON configs

### History Module (`history.py`)
- **HistoryManager class**: Tracks converted files and user settings
- **Settings**: output_folder, source_folder, auto_detect, delete_duplicates
- **Conversion history**: Stores source filename, MD5 hash, output filename, timestamp, filament count
- **Duplicate detection**: MD5 hash-based to skip exact duplicates or create versioned files

### Frontend (`templates/index.html`)
- Single-page app using Tailwind CSS (CDN) and Font Awesome
- **Single File Mode**: upload → configure filaments → download
- **Batch Mode**: select folder → preview files → convert all → files saved to output folder
- **Settings Panel**: Configure output/source folders, auto-detect new files, duplicate handling
- **Folder Browser**: Navigate server filesystem to select folders
- **New Files Badge**: Shows count of unconverted files in source folder

### Template 3MF Files
- `u1_template.3mf` - Base U1 printer profile (supports disabled)
- `u1_template_supports.3mf` - U1 profile with Tree Supports (auto) enabled
- `filament_types.3mf` - Reference file containing available Snapmaker U1 filament profiles

### Conversion Logic (in `convert_single_file()`)
1. Reads original Bambu .3mf and extracts project settings
2. **Multi-plate → ONE multi-plate U1 file** (`merge_plates=True`, default). Snapmaker Orca keeps all plates in one file arranged in a grid, so the converter keeps every plate and recenters each onto its Orca grid cell (`plate_cell_center`; stride = `bed.width * 1.2` = 324 mm for the 270 mm bed; columns via `compute_plate_cols`, a round-sqrt rule; cell = `(135.5 + col·324, 136 − row·324)`, verified to the decimal against Orca's own U1 files at N=2/5/6/8/10). `wipe_tower_x/y` are resized to one per plate (`_resize_plate_settings`); stale per-plate `Metadata/plate_*.json` caches are dropped. `merge_plates=False` (the legacy split path) still refuses >1 plate.
3. Detects if supports were enabled via `different_settings_to_system` array
4. Selects appropriate U1 template based on support detection
5. **Derives bed bounds from the selected template** (`parse_printable_area`) — never hard-coded
6. Modifies internal configs:
   - `Metadata/slice_info.config` (XML) - printer model, filament mappings (when present)
   - `Metadata/model_settings.config` (XML) - extruder references for painted regions (remapped via `id_mapping`; an unmapped reference raises rather than corrupting color)
   - `Metadata/project_settings.config` (JSON) - printer settings, filament colors/types
   - `3D/3dmodel.model` (XML) - group-recenter + per-item drop-to-bed
7. **Filament count = the source's color count (min 4), fully consistent.** The U1 prints >4 colors (4 tools + swaps), so the output is NOT capped at 4 — it carries N filaments (padded up to 4 with white PLA when fewer). `_resize_filament_settings()` resizes **every** per-filament array in `project_settings.config` to N — both `filament_*` keys AND the ~51 per-filament keys that lack that prefix (`nozzle_temperature*`, plate temps, fan speeds, `pressure_advance`, `required_nozzle_HRC`, …, in `U1_PER_FILAMENT_EXTRA_KEYS`) — and rebuilds the inter-filament flush **matrix** (N×N, diagonal `'0'`, top-left 4×4 preserved, new pairs `'280'`) and **vector** (2N). Per-**extruder** arrays (`nozzle_diameter`, `extruder_*`, `retraction_*`, `wipe*`, `z_hop*`) stay at 4; per-**plate** `wipe_tower_x/y` untouched. Previously only `filament_*` was extended, leaving temps/matrix at 4 → Snapmaker Orca rejected >4-color files. Key classification is ground-truthed against Orca's own re-saved U1 files at N=5/6/7; verified in `test_filament_arrays.py`.
   - **Source process settings preserved** (`U1_PRESERVE_FROM_SOURCE`): `layer_height`, `initial_layer_print_height`, and `single_extruder_multi_material` are carried over from the source, NOT taken from the U1 template — they define how the model slices, not the printer. (1) Layer heights set the model resolution AND the Z-grid that `Metadata/custom_gcode_per_layer.xml` by-height tool-changes align to — template's 0.2/0.25 halved a 0.08mm lithophane (20→8 layers) and knocked every color change off its boundary. (2) `single_extruder_multi_material='1'` means color changes happen via filament SWAP on one nozzle (`custom_gcode` uses `mode="MultiAsSingle"`); the template's `'0'` made Orca treat it as multi-tool and **silently drop every by-layer tool-change** → multi-color print came out one color. Verified in `test_recenter.py`.
8. Writes new .3mf archive

### Auto-Center / Drop-to-Bed Feature (`recenter_and_drop_model()`)
- **Problem**: Bambu files are laid out for a larger (or multi-plate) canvas; the U1 prints one plate on a rectangular printable area. The center is **not** square-230.
- **Bed bounds are parsed from the template's `printable_area` polygon** → bounds `[0.5, 270.5] × [1, 271]`, center **(135.5, 136.0)**. Independent X/Y always. (`U1_BED_SIZE`/`U1_BED_CENTER` constants were removed — they encoded a wrong 230/115 square bed that placed models off-bed and could crash the printer with "Move out of range".)
- **Group recenter, not per-item**: computes the mesh-derived global XY bounding box over **all** build items (resolving `<component>` submodels through their transforms), then applies a **single rigid delta** so the group's bbox center lands on the bed center. Every item's rotation/scale and all inter-item offsets are preserved (multi-object layouts stay intact instead of collapsing onto one point).
- **Per-item drop-to-bed**: each item's Z translation is reduced by its own world-space minimum Z, so each part sits on the bed **without** flattening intentional relative Z between parts (the old blanket zeroing of every part matrix's `m23` destroyed that).
- **Streaming geometry index (perf)**: recenter only needs each object's local AABB + its `<component>` list, never triangles. Large mesh submodels (100+ MB, millions of `<triangle>`s) are read via `_stream_geom_index` (`ET.iterparse` + bounded-memory child pruning) into a `_GeomIndex` `{oid: {aabb, components}}` — parsed **once**, not built into a full DOM and re-walked. `make_zip_submodel_reader(zin)` is the shared submodel-reader factory (used by both `convert_single_file` and `refix.py`). `_collect_global_corners` consumes the index; `_as_geom_index` transparently accepts either a `_GeomIndex`, a parsed root (for the small main model + tests), or `None`. Cut a real 108 MB-mesh lithophane plate from ~45s → ~19s convert; DOM/stream index parity is asserted in `test_recenter.py`.
- **Fail-loud**: raises `ConversionError` on malformed transforms (naming the object + offending token), no resolvable geometry, or a group that cannot fit the printable area. Helpers raise; `convert_single_file` catches at its boundary → `(False, message)` + logged traceback.

### Geometry-Only Re-fix (`refix.py`)
- **Purpose**: re-apply ONLY the bed placement (group recenter + drop-to-bed) to a `.3mf`, copying every other archive entry byte-for-byte. Filament colors/types/painting and all metadata are preserved exactly — no filament UI, no id remapping. Fixes an already-converted U1 file that sits off-bed **without** redoing custom ("full spectrum") colors.
- **`refix_geometry_only(input_path, output_path, template_file)`**: reuses `recenter_and_drop_model` / `parse_printable_area` (no duplicated logic); rewrites only `3D/3dmodel.model`; refuses multi-plate. Bed bounds from the file's own `printable_area` if present, else the template.
- **CLI**: `python refix.py FILE_OR_DIR [...] [--inplace] [--suffix _fixed]` — default writes `<name>_fixed.3mf`; `--inplace` overwrites after a one-time `<name>.bak`.

### Per-plate Split (`split_plates.py`)
- **Purpose**: turn one multi-plate Bambu `.3mf` into N single-plate `.3mf` files (`<stem>_plate{k}.3mf`, k=1..N), each of which then converts normally via `convert_single_file`.
- **`split_plates(input_path, output_dir=None) -> (ok, list[str] | msg)`**: imports helpers from `app` (`count_plates`, `_collect_build_items`, `CORE_NS`) — no duplicated logic. Per output only two entries are rewritten: `Metadata/model_settings.config` keeps ONLY that plate's `<plate>` block, its `<object>` config blocks, and its `assemble_item`s; `3D/3dmodel.model` build is filtered to only that plate's `<item>`s (resources untouched). **Every other archive entry is copied byte-for-byte** (colors/painting preserved).
- **Raw member copy (perf)**: unchanged entries are transferred via `_copy_member_raw` — the source's already-compressed bytes are written verbatim (regenerating a clean local header from the central-directory `ZipInfo`, streaming data-descriptor bit cleared), instead of decompress→re-deflate. The huge mesh is identical in every plate output, so this avoids re-deflating 100+ MB per plate; guards fall back to decompress+recompress for encrypted/ZIP64 members. Cut a 2-plate 108 MB-mesh split from ~22s → ~0.5s (verified `testzip` OK + members byte-identical).
- **Plate membership**: each `<plate>` block's `<model_instance>` children carry `<metadata key="object_id">` values matching build `<item objectid>` (verified against real 7-plate BambuStudio output).
- **Fail-loud**: single-plate input, a plate with no `model_instance` objects, or an object_id with no matching build item all return `(False, msg)` BEFORE any output is written (no partial output set). Note: `zipfile.writestr` mutates a passed `ZipInfo`; the splitter writes with copies so the input infolist survives multiple output passes.
- **CLI**: `python split_plates.py FILE [--outdir DIR]` — default writes beside the input.
- **Web `/convert` uses it automatically** via `convert_or_split_plates(input, output, colors, base_name, save_dir)`: single-plate → one `_U1.3mf`; multi-plate → **one multi-plate `_U1.3mf`** (all plates in the Orca grid). Only if the one-file convert fails (e.g. a plate too big for the bed) does it **fall back** to the legacy split path (split + convert each → **ZIP** of `<name>_plate{k}_U1.3mf`, partial success + `warning`). A multi-plate file is never refused in the UI. If `save_dir` (the Settings `output_folder`) is set, the print-ready file(s) are also copied there. NB: the routes pass a temp `output_path` (in `uploads/`), never inside `save_dir` — `_clear_stale_outputs` runs before `_save`, so an `output_path` inside `save_dir` would be cleared before copying.
- **Stale-output cleanup**: before saving into `save_dir`, `_clear_stale_outputs(save_dir, base_name)` deletes that base name's prior outputs (`<base>_U1.3mf`, `<base>_U1.zip`, `<base>_plate<N>_U1.3mf`) so a re-conversion that yields fewer plates never leaves stale plate files behind (e.g. old `_plate5..8`, or a leftover from an earlier buggy run that opens as "no geometry"). Runs only on success; matches this base name exactly and never touches other files.

### Duplicate Handling (`dedup.py`)
- **No more `_v2`/`_v3` outputs**: the batch routes (`/batch-convert`, `/convert-new`, `/convert-file`) used to append `_U1_v{N}` whenever the target name existed, producing byte-identical copies on re-conversion. They now write one deterministic `<base>_U1.3mf` and **overwrite** it (the interactive `/convert` route already overwrites via `_clear_stale_outputs`).
- **`dedup.py`**: `find_duplicate_groups(folder, pattern)` / `dedup_folder(folder, pattern, apply)` group files by **content hash** (size prefilter → md5) and keep one canonical name per group (prefers no `_v<N>` suffix, then no ` (N)` copy suffix, then shortest, then oldest); removing a byte-identical dup is lossless. CLI: `python dedup.py FOLDER [--pattern '*.3mf'] [--apply]` (dry run without `--apply`).
- **In-app**: `POST /dedup-outputs {apply: bool}` dedupes the Settings `output_folder` and returns a report; the Settings panel has a **"Remove duplicate files"** button (dry-run → confirm → apply).

### Batch Conversion
- **`is_bambu_file(filepath)`**: Checks if a .3mf is from Bambu Lab (not already Snapmaker)
- **`auto_map_filaments(filaments)`**: Automatically maps filament types to closest U1 profiles (PLA→PLA, PETG→PETG-HF, etc.)
- **Workflow**: Upload folder → Filter to valid Bambu files → Auto-map filaments → Convert all → Create ZIP archive
- **Output**: Each file renamed `original_name_U1.3mf`, bundled in a single ZIP download

### Key Data Structures
- Filament mapping: `{original_id: {color: "#RRGGBB", type: "PLA"}}` passed from frontend
- ID remapping: Original Bambu filament IDs → sequential 1-based U1 IDs (critical for extruder references)
- Batch files list: `[{filename, filaments, auto_colors}, ...]` for batch preview

### Testing
```bash
# Run Playwright UI tests (21 tests, needs a live server on :8085 + chromium)
python3 -m pytest test_batch.py -v

# Run History module tests (8 tests)
python3 -m pytest test_history.py -v

# Run recenter / bed-dims / drop-to-bed + fail-loud + streaming-index tests (28 tests, pure Python)
python3 -m pytest test_recenter.py -v

# Run per-plate split tests (7 tests, pure Python)
python3 -m pytest test_split.py -v

# Run all tests
python3 -m pytest -v
```

`test_recenter.py` builds minimal in-memory 3MF models and a synthetic
single-plate fixture (no server/browser needed). It asserts: group recenter to
the template-derived bed center, relative offsets and part-Z preserved, delta
applied exactly once, multi-plate refusal, fail-loud on malformed transforms /
oversize groups, and that `id_mapping` is built even when `slice_info.config`
carries no `<filament>` nodes (extruders still remap; uncovered regions raise).


## ECC Integration

**Rule packs in effect:** `common`, `python`, `web`.

**Primary skills:**
- `documentation-lookup` — .3mf spec, Bambu Lab project metadata, Snapmaker U1 filament metadata
- `tdd` — `test_batch.py` exists; lean on it. Conversion correctness is testable on fixture files.
- `regex-vs-llm-structured-text` — .3mf is XML; parse it deterministically
- `python-fastapi` rule isn't a 1:1 match (this is Flask) but `python/patterns.md` still applies
- `search-first` — `filament_types.3mf` is the truth table; check it before adding new filament inference

**Commands:**
- `/ecc:plan` before changing the converter pipeline or filament-mapping logic
- `/ecc:harness-audit` to capture Docker build/run and `batch_cli.py` invocation
- `/ecc:quality-gate` on every commit; conversion bugs corrupt user files

**Project-specific instincts:**
1. **`filament_types.3mf` is the reference filament table.** Don't hardcode filament metadata elsewhere.
2. **Multi-color painting and filament assignments are the differentiator.** Conversion fidelity here is the product. Test on fixtures with multi-color paint before any release.
3. **`conversion_history.json` is the user-facing log.** Keep schema stable.
4. **Batch CLI and Flask app share core converter.** Don't duplicate conversion logic across the two surfaces.
5. **User-uploaded files = untrusted XML.** Use a hardened parser; never `eval`, never let XML reference external entities.

**Verification before shipping:**
1. `python test_batch.py` and any pytest suite green.
2. Convert at least one known-good .3mf and one known-bad .3mf; verify outputs.
3. `/ecc:quality-gate`.
4. `/ecc:security-scan` — Flask + file upload + XML parsing = three classic vuln surfaces.
