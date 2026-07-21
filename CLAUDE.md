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
2. **Refuses multi-plate files loudly**: counts `<plate>` blocks in `model_settings.config`; a file with >1 plate is rejected with a clear error (the U1 prints one plate). Per-plate splitting is a planned follow-up.
3. Detects if supports were enabled via `different_settings_to_system` array
4. Selects appropriate U1 template based on support detection
5. **Derives bed bounds from the selected template** (`parse_printable_area`) — never hard-coded
6. Modifies internal configs:
   - `Metadata/slice_info.config` (XML) - printer model, filament mappings (when present)
   - `Metadata/model_settings.config` (XML) - extruder references for painted regions (remapped via `id_mapping`; an unmapped reference raises rather than corrupting color)
   - `Metadata/project_settings.config` (JSON) - printer settings, filament colors/types
   - `3D/3dmodel.model` (XML) - group-recenter + per-item drop-to-bed
7. Pads to 4 filaments (U1 hardware requirement) with white PLA
8. Writes new .3mf archive

### Auto-Center / Drop-to-Bed Feature (`recenter_and_drop_model()`)
- **Problem**: Bambu files are laid out for a larger (or multi-plate) canvas; the U1 prints one plate on a rectangular printable area. The center is **not** square-230.
- **Bed bounds are parsed from the template's `printable_area` polygon** → bounds `[0.5, 270.5] × [1, 271]`, center **(135.5, 136.0)**. Independent X/Y always. (`U1_BED_SIZE`/`U1_BED_CENTER` constants were removed — they encoded a wrong 230/115 square bed that placed models off-bed and could crash the printer with "Move out of range".)
- **Group recenter, not per-item**: computes the mesh-derived global XY bounding box over **all** build items (resolving `<component>` submodels through their transforms), then applies a **single rigid delta** so the group's bbox center lands on the bed center. Every item's rotation/scale and all inter-item offsets are preserved (multi-object layouts stay intact instead of collapsing onto one point).
- **Per-item drop-to-bed**: each item's Z translation is reduced by its own world-space minimum Z, so each part sits on the bed **without** flattening intentional relative Z between parts (the old blanket zeroing of every part matrix's `m23` destroyed that).
- **Fail-loud**: raises `ConversionError` on malformed transforms (naming the object + offending token), no resolvable geometry, or a group that cannot fit the printable area. Helpers raise; `convert_single_file` catches at its boundary → `(False, message)` + logged traceback.

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

# Run recenter / bed-dims / drop-to-bed + fail-loud tests (15 tests, pure Python)
python3 -m pytest test_recenter.py -v

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
