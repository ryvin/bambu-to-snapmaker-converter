#!/usr/bin/env python3
"""
Per-plate splitter for multi-plate Bambu .3mf files.

The U1 prints one plate, so the converter refuses multi-plate files. This tool
turns one multi-plate file into N single-plate files (``<stem>_plateK.3mf``),
each of which then converts normally through ``convert_single_file``.

Per output only two entries are rewritten:
  - ``Metadata/model_settings.config``: keeps ONLY that plate's ``<plate>``
    block, that plate's ``<object>`` config blocks, and its ``assemble_item``
    entries; other plates' blocks are dropped.
  - ``3D/3dmodel.model``: build filtered to only the ``<item>`` entries whose
    ``objectid`` belongs to that plate (resources untouched).
Every other archive entry is copied byte-for-byte — colors, painting, and all
other metadata are preserved exactly.

Plate membership: each ``<plate>`` block's ``<model_instance>`` children carry
``<metadata key="object_id" value="...">`` matching the ``objectid`` attribute
of build ``<item>`` entries (verified against real 7-plate BambuStudio output).

Fail-loud policy: single-plate input, a plate with no model_instance objects,
or an object_id with no matching build item all return ``(False, message)``
BEFORE any output is written.

CLI:
    python split_plates.py FILE [--outdir DIR]
"""
import argparse
import copy
import os
import shutil
import sys
import traceback
import zipfile
import xml.etree.ElementTree as ET

from app import (
    ConversionError,
    CORE_NS,
    PROD_NS,
    count_plates,
    _collect_build_items,
    _lname,
)

BAMBU_NS = 'http://schemas.bambulab.com/package/2021'


def _plate_object_ids(plate_elem):
    """object_id values listed by a <plate> block's <model_instance> children."""
    ids = []
    for mi in plate_elem.findall('model_instance'):
        for md in mi.findall('metadata'):
            if md.get('key') == 'object_id':
                ids.append(md.get('value'))
    return ids


def _filter_model_settings(ms_root, keep_plate_idx, keep_ids):
    """
    Deep-copied model_settings root containing only plate ``keep_plate_idx``
    (0-based document order), the <object> config blocks for ``keep_ids``, and
    the assemble_item entries for ``keep_ids``.
    """
    root = copy.deepcopy(ms_root)
    plates = root.findall('.//plate')
    drop_plates = {
        id(p) for i, p in enumerate(plates) if i != keep_plate_idx
    }
    for parent in list(root.iter()):
        for child in list(parent):
            if id(child) in drop_plates:
                parent.remove(child)
            elif (parent is root and child.tag == 'object'
                  and child.get('id') not in keep_ids):
                parent.remove(child)
            elif (child.tag == 'assemble_item'
                  and child.get('object_id') not in keep_ids):
                parent.remove(child)
    return root


def _filter_build_items(model_root, keep_ids):
    """Deep-copied 3dmodel root whose build keeps only items in keep_ids."""
    root = copy.deepcopy(model_root)
    for elem in root.iter():
        if _lname(elem.tag) != 'build':
            continue
        for item in list(elem):
            if (_lname(item.tag) == 'item'
                    and item.get('objectid') not in keep_ids):
                elem.remove(item)
    return root


def split_plates(input_path, output_dir=None):
    """
    Split a multi-plate .3mf into one single-plate .3mf per plate.

    Returns (True, [output_paths]) on success, else (False, message).
    Outputs are named ``<stem>_plate{k}.3mf`` (k = 1..N in document order) and
    written into ``output_dir`` (created if needed) or beside the input.
    """
    src_name = os.path.basename(input_path)
    stem = os.path.splitext(src_name)[0]
    out_dir = output_dir or os.path.dirname(os.path.abspath(input_path))

    temp_paths = []
    try:
        with zipfile.ZipFile(input_path, 'r') as zin:
            names = zin.namelist()
            if '3D/3dmodel.model' not in names:
                return (False, f"{src_name} has no 3D/3dmodel.model; not a valid 3MF.")
            if 'Metadata/model_settings.config' not in names:
                return (False,
                        f"{src_name} has no Metadata/model_settings.config; "
                        f"cannot determine plate membership.")

            ms_root = ET.fromstring(
                zin.read('Metadata/model_settings.config').decode('utf-8')
            )
            n_plates = count_plates(ms_root)
            if n_plates <= 1:
                return (False,
                        f"{src_name} contains {n_plates} plate(s); nothing to "
                        f"split — convert it directly.")

            # Preserve namespaces on the rewritten 3D model.
            for prefix, uri in {'': CORE_NS, 'p': PROD_NS,
                                'BambuStudio': BAMBU_NS}.items():
                ET.register_namespace(prefix, uri)
            model_root = ET.fromstring(zin.read('3D/3dmodel.model').decode('utf-8'))
            build_ids = {it.get('objectid') for it in _collect_build_items(model_root)}

            # Validate EVERY plate before writing anything (fail loud, no
            # partial output set).
            plates = ms_root.findall('.//plate')
            plate_ids = []
            for k, plate in enumerate(plates, start=1):
                ids = _plate_object_ids(plate)
                if not ids:
                    return (False,
                            f"{src_name}: plate {k} lists no model_instance "
                            f"objects; there are no build items to split out.")
                missing = sorted(i for i in ids if i not in build_ids)
                if missing:
                    return (False,
                            f"{src_name}: plate {k} references object_id(s) "
                            f"{missing} with no matching build <item> in "
                            f"3D/3dmodel.model; the file is inconsistent.")
                plate_ids.append(ids)

            os.makedirs(out_dir, exist_ok=True)
            outputs = []
            for k, ids in enumerate(plate_ids, start=1):
                keep = set(ids)
                new_ms = ET.tostring(
                    _filter_model_settings(ms_root, k - 1, keep),
                    encoding='utf-8', xml_declaration=True,
                )
                new_model = ET.tostring(
                    _filter_build_items(model_root, keep),
                    encoding='utf-8', xml_declaration=True,
                )

                out_path = os.path.join(out_dir, f"{stem}_plate{k}.3mf")
                temp_path = out_path + '.temp'
                temp_paths.append(temp_path)
                with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                    for info in zin.infolist():
                        # writestr MUTATES the ZipInfo it is given (header
                        # offset/sizes for the new archive); pass a copy so
                        # zin's infolist stays valid for the next plate.
                        if info.filename == 'Metadata/model_settings.config':
                            zout.writestr(copy.copy(info), new_ms)
                        elif info.filename == '3D/3dmodel.model':
                            zout.writestr(copy.copy(info), new_model)
                        else:
                            zout.writestr(copy.copy(info), zin.read(info.filename))
                outputs.append(out_path)

        # All plates written cleanly; publish atomically.
        for temp_path, out_path in zip(temp_paths, outputs):
            shutil.move(temp_path, out_path)
        return (True, outputs)

    except (ConversionError, ET.ParseError, zipfile.BadZipFile, OSError) as e:
        for temp_path in temp_paths:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        traceback.print_exc()
        return (False, f"{src_name}: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Split a multi-plate Bambu .3mf into one file per plate."
    )
    ap.add_argument('file', help="Multi-plate .3mf to split.")
    ap.add_argument('--outdir', default=None,
                    help="Directory for the <stem>_plateK.3mf outputs "
                         "(default: beside the input).")
    args = ap.parse_args(argv)

    ok, result = split_plates(args.file, args.outdir)
    if ok:
        for p in result:
            print(f"OK   {p}", flush=True)
        print(f"\n{len(result)} plates written.")
        return 0
    print(f"ERROR: {result}", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
