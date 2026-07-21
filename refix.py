#!/usr/bin/env python3
"""
Geometry-only re-fix for Snapmaker U1 .3mf files.

Re-applies ONLY the bed placement (group recenter + per-item drop-to-bed) to a
.3mf while copying every other archive entry byte-for-byte. Filament colors,
types, painting, and all metadata are preserved exactly — no filament UI, no id
remapping. Use this to fix an already-converted U1 file whose model sits off the
bed WITHOUT redoing custom ("full spectrum") color setup.

It reuses the same fixed geometry code as the main converter
(``recenter_and_drop_model`` / ``parse_printable_area`` in app.py), so there is
no duplicated conversion logic.

CLI:
    python refix.py FILE_OR_DIR [FILE_OR_DIR ...] [--inplace] [--suffix _fixed]

Default writes ``<name>_fixed.3mf`` beside each input. ``--inplace`` overwrites
the original after saving a one-time ``<name>.bak`` next to it.
"""
import os
import sys
import json
import shutil
import zipfile
import argparse
import traceback
import xml.etree.ElementTree as ET

from app import (
    ConversionError,
    CORE_NS,
    PROD_NS,
    parse_printable_area,
    recenter_and_drop_model,
    count_plates,
)

BAMBU_NS = 'http://schemas.bambulab.com/package/2021'
DEFAULT_TEMPLATE = 'u1_template.3mf'


def refix_geometry_only(input_path, output_path, template_file=DEFAULT_TEMPLATE):
    """
    Rewrite only 3D/3dmodel.model placement; copy all other entries unchanged.

    Bed bounds come from the file's own project_settings ``printable_area`` when
    present, else from ``template_file``. Multi-plate files are refused (their
    layout cannot be recovered from a single shared canvas). Returns
    (success, error_message).
    """
    src_name = os.path.basename(input_path)
    temp_zip = output_path + '.temp'
    try:
        with zipfile.ZipFile(input_path, 'r') as zin:
            names = zin.namelist()
            if '3D/3dmodel.model' not in names:
                return (False, f"{src_name} has no 3D/3dmodel.model; not a valid 3MF.")

            # Refuse multi-plate (same policy as convert_single_file).
            if 'Metadata/model_settings.config' in names:
                ms_root = ET.fromstring(
                    zin.read('Metadata/model_settings.config').decode('utf-8')
                )
                plate_count = count_plates(ms_root)
                if plate_count > 1:
                    return (
                        False,
                        f"{src_name} contains {plate_count} plates; the U1 prints "
                        f"one plate — re-export a single plate",
                    )

            # This tool only RE-PLACES a genuine U1 conversion. A file still on a
            # Bambu (or other) printer profile needs FULL re-conversion to swap
            # the profile, which geometry-only refix cannot do — refuse it so we
            # never hand back a mis-profiled file that looks "fixed".
            ps = {}
            if 'Metadata/project_settings.config' in names:
                try:
                    ps = json.loads(
                        zin.read('Metadata/project_settings.config').decode('utf-8')
                    )
                except json.JSONDecodeError:
                    ps = {}
            profile = str(ps.get('printer_settings_id') or ps.get('printer_model') or '')
            if 'U1' not in profile and 'Snapmaker' not in profile:
                return (
                    False,
                    f"{src_name} is not a Snapmaker U1 conversion (printer profile "
                    f"'{profile or 'unknown'}'); re-convert it through the app to swap "
                    f"the profile — colors are preserved.",
                )

            # Always target the authoritative U1 bed from the template, never the
            # file's own printable_area (a mis-set bed would place the model wrong).
            with zipfile.ZipFile(template_file, 'r') as zt:
                bed = parse_printable_area(
                    json.loads(
                        zt.read('Metadata/project_settings.config').decode('utf-8')
                    )
                )

            # Preserve namespaces on output.
            for prefix, uri in {'': CORE_NS, 'p': PROD_NS, 'BambuStudio': BAMBU_NS}.items():
                ET.register_namespace(prefix, uri)

            model_root = ET.fromstring(zin.read('3D/3dmodel.model').decode('utf-8'))

            _cache = {}

            def _read_submodel(path):
                name = str(path).lstrip('/')
                if name in _cache:
                    return _cache[name]
                root = None
                if name in names:
                    try:
                        root = ET.fromstring(zin.read(name).decode('utf-8'))
                    except ET.ParseError as e:
                        raise ConversionError(f"Corrupt submodel '{name}': {e}")
                _cache[name] = root
                return root

            recenter_and_drop_model(model_root, _read_submodel, bed)
            new_model = ET.tostring(
                model_root, encoding='utf-8', xml_declaration=True
            )

            # Copy every entry unchanged except the rewritten model.
            with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if info.filename == '3D/3dmodel.model':
                        zout.writestr(info, new_model)
                    else:
                        zout.writestr(info, zin.read(info.filename))

        shutil.move(temp_zip, output_path)
        return (True, None)

    except ConversionError as e:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        traceback.print_exc()
        return (False, str(e))
    except Exception as e:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        traceback.print_exc()
        return (False, str(e))


def _iter_targets(paths):
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith('.3mf'):
                    yield os.path.join(p, name)
        else:
            yield p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Geometry-only U1 .3mf re-fix (keeps colors).")
    ap.add_argument('paths', nargs='+', help="Files or directories of .3mf to re-fix.")
    ap.add_argument('--inplace', action='store_true',
                    help="Overwrite the original (keeps a one-time <name>.bak).")
    ap.add_argument('--outdir', default=None,
                    help="Write fixed files (same basename) into this directory; "
                         "originals untouched. Overrides --suffix/--inplace.")
    ap.add_argument('--suffix', default='_fixed',
                    help="Output suffix when not --inplace/--outdir (default: _fixed).")
    args = ap.parse_args(argv)

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    ok_n = fail_n = 0
    for src in _iter_targets(args.paths):
        stem, ext = os.path.splitext(src)
        if args.outdir:
            out = os.path.join(args.outdir, os.path.basename(src))
            success, err = refix_geometry_only(src, out)
        elif args.inplace:
            out = src
            bak = src + '.bak'
            if not os.path.exists(bak):
                shutil.copy(src, bak)
            tmp = stem + args.suffix + ext
            success, err = refix_geometry_only(src, tmp)
            if success:
                shutil.move(tmp, out)
        else:
            out = stem + args.suffix + ext
            success, err = refix_geometry_only(src, out)

        if success:
            ok_n += 1
            print(f"OK   {os.path.basename(src)} -> {out}", flush=True)
        else:
            fail_n += 1
            print(f"SKIP {os.path.basename(src)}: {err}", flush=True)

    print(f"\n{ok_n} fixed, {fail_n} skipped.")
    return 0 if fail_n == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
