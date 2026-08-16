#!/usr/bin/env python3
"""
Tests for the per-plate splitter (split_plates.py).

Pure-Python tests (no Flask server, no browser). They build a synthetic
multi-plate Bambu-style .3mf where each plate owns DISTINCT objects, split it,
and assert: correct output naming, single-plate outputs with only their own
build items, byte-identical non-model entries (colors/painting untouched),
successful downstream conversion via convert_single_file, refusal of
single-plate input, and loud failure on unresolvable object_ids.

Run inside the container:
    docker exec bambu-to-u1-converter python -m pytest /app/test_split.py -v
"""
import hashlib
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

from app import (
    convert_single_file,
    convert_or_split_plates,
    count_plates,
    _collect_build_items,
    CORE_NS,
    PROD_NS,
)
from split_plates import split_plates


# ---------------------------------------------------------------------------
# Synthetic multi-plate fixture: plate k owns objects str(100*k) & str(100*k+1)
# ---------------------------------------------------------------------------
def _mesh_xml(aabb):
    """Emit a <mesh> whose 8 corner vertices span the given AABB."""
    minx, miny, minz, maxx, maxy, maxz = aabb
    verts = []
    for x in (minx, maxx):
        for y in (miny, maxy):
            for z in (minz, maxz):
                verts.append(f'<vertex x="{x}" y="{y}" z="{z}"/>')
    return "<mesh><vertices>" + "".join(verts) + "</vertices><triangles/></mesh>"


def plate_object_ids(k):
    """The two distinct object ids owned by plate k in the fixture."""
    return [str(100 * k), str(100 * k + 1)]


def make_multi_plate_3mf(path, plates=3, bogus_object_in_plate=None,
                         empty_plate=None):
    """
    Build a minimal valid multi-plate Bambu-style .3mf. Each plate k owns two
    DISTINCT objects (ids 100k and 100k+1) with direct meshes, present in
    resources + build + model_settings (object config blocks with extruder
    refs 1/2, plate blocks with model_instance object_id entries, and an
    assemble section — mirroring real BambuStudio output).

    ``bogus_object_in_plate=k`` adds a model_instance for object_id 9999 (no
    matching build item) to plate k. ``empty_plate=k`` emits plate k with NO
    model_instance children.
    """
    all_ids = [oid for k in range(1, plates + 1) for oid in plate_object_ids(k)]

    obj_xml = ""
    item_xml = ""
    for k in range(1, plates + 1):
        oid_a, oid_b = plate_object_ids(k)
        obj_xml += (
            f'<object id="{oid_a}" type="model">{_mesh_xml((0, 0, 0, 10, 10, 10))}</object>'
            f'<object id="{oid_b}" type="model">{_mesh_xml((0, 0, 0, 20, 20, 20))}</object>'
        )
        # Distinct XY per plate, like a real multi-plate canvas layout.
        x = 300 + 60 * k
        item_xml += (
            f'<item objectid="{oid_a}" transform="1 0 0 0 1 0 0 0 1 {x} 300 2" printable="1"/>'
            f'<item objectid="{oid_b}" transform="1 0 0 0 1 0 0 0 1 {x + 25} 300 2" printable="1"/>'
        )

    main = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}" '
        f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">'
        f"<resources>{obj_xml}</resources>"
        f"<build>{item_xml}</build></model>"
    )

    object_blocks = ""
    for k in range(1, plates + 1):
        oid_a, oid_b = plate_object_ids(k)
        object_blocks += (
            f'<object id="{oid_a}">'
            f'<metadata key="name" value="obj {oid_a}"/>'
            f'<metadata key="extruder" value="1"/>'
            f"</object>"
            f'<object id="{oid_b}">'
            f'<metadata key="name" value="obj {oid_b}"/>'
            f'<metadata key="extruder" value="2"/>'
            f"</object>"
        )

    plate_blocks = ""
    for k in range(1, plates + 1):
        instances = ""
        if empty_plate != k:
            for oid in plate_object_ids(k):
                instances += (
                    f'<model_instance><metadata key="object_id" value="{oid}"/>'
                    f'<metadata key="instance_id" value="0"/></model_instance>'
                )
            if bogus_object_in_plate == k:
                instances += (
                    f'<model_instance><metadata key="object_id" value="9999"/>'
                    f'<metadata key="instance_id" value="0"/></model_instance>'
                )
        plate_blocks += (
            f"<plate>"
            f'<metadata key="plater_id" value="{k}"/>'
            f'<metadata key="plater_name" value=""/>'
            f"{instances}</plate>"
        )

    assemble = (
        "<assemble>"
        + "".join(
            f'<assemble_item object_id="{oid}" instance_id="0" '
            f'transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0"/>'
            for oid in all_ids
        )
        + "</assemble>"
    )

    model_settings = (
        f'<?xml version="1.0" encoding="UTF-8"?><config>'
        f"{object_blocks}{plate_blocks}{assemble}</config>"
    )

    slice_info = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        '<header><header_item key="X-BBL-Client-Type" value="slicer"/></header>'
        "<plate>"
        '<metadata key="index" value="1"/>'
        '<metadata key="printer_model_id" value="Bambu Lab X1 Carbon"/>'
        '<filament id="1" tray_info_idx="GFA00" type="PLA" color="#FF0000" used_m="1" used_g="1"/>'
        '<filament id="2" tray_info_idx="GFA01" type="PLA" color="#00FF00" used_m="1" used_g="1"/>'
        "</plate></config>"
    )

    project_settings = json.dumps(
        {
            "printer_model": "Bambu Lab X1 Carbon",
            "printer_settings_id": "Bambu Lab X1 Carbon",
            "different_settings_to_system": [],
            "filament_colour": ["#FF0000", "#00FF00"],
            "filament_type": ["PLA", "PLA"],
        }
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", main)
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/project_settings.config", project_settings)
        # Untouchable payload entries (stand-ins for thumbnails/paint data).
        z.writestr("Metadata/plate_1.png", b"\x89PNG-fake-1")
        z.writestr("Auxiliaries/pic.webp", b"webp-fake-bytes")


DEFAULT_COLORS = {
    "1": {"color": "#FF0000FF", "type": "PLA"},
    "2": {"color": "#00FF00FF", "type": "PLA"},
}

N_PLATES = 3


@pytest.fixture
def split_fixture(tmp_path):
    src = str(tmp_path / "multi.3mf")
    make_multi_plate_3mf(src, plates=N_PLATES)
    ok, result = split_plates(src, str(tmp_path))
    assert ok, result
    return src, result, tmp_path


# ===========================================================================
# (a) N outputs produced, correctly named
# ===========================================================================
def test_split_produces_n_named_outputs(split_fixture):
    src, outputs, tmp_path = split_fixture
    assert len(outputs) == N_PLATES
    expected = [str(tmp_path / f"multi_plate{k}.3mf") for k in range(1, N_PLATES + 1)]
    assert outputs == expected
    for p in outputs:
        assert os.path.exists(p)


# ===========================================================================
# (b) each output: exactly 1 plate, only its own build items + object blocks
# ===========================================================================
def test_each_output_single_plate_with_own_items(split_fixture):
    _, outputs, _ = split_fixture
    for k, out in enumerate(outputs, start=1):
        with zipfile.ZipFile(out) as z:
            ms = ET.fromstring(z.read("Metadata/model_settings.config").decode("utf-8"))
            model = ET.fromstring(z.read("3D/3dmodel.model").decode("utf-8"))

        assert count_plates(ms) == 1

        # The surviving plate is plate k (plater_id preserved).
        plate = ms.find(".//plate")
        plater_id = {m.get("key"): m.get("value") for m in plate.findall("metadata")}["plater_id"]
        assert plater_id == str(k)

        # Build contains exactly this plate's items, nothing else.
        build_ids = sorted(it.get("objectid") for it in _collect_build_items(model))
        assert build_ids == sorted(plate_object_ids(k))

        # model_settings keeps only this plate's object config blocks.
        cfg_ids = sorted(o.get("id") for o in ms.findall("object"))
        assert cfg_ids == sorted(plate_object_ids(k))


# ===========================================================================
# (c) every non-model entry is byte-identical to the input
# ===========================================================================
def test_non_model_entries_byte_identical(split_fixture):
    src, outputs, _ = split_fixture

    def entry_hashes(path):
        with zipfile.ZipFile(path) as z:
            return {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist()}

    before = entry_hashes(src)
    rewritten = {"Metadata/model_settings.config", "3D/3dmodel.model"}
    for out in outputs:
        after = entry_hashes(out)
        assert set(after) == set(before)  # no entries added or lost
        changed = {n for n in before if before[n] != after[n]}
        assert changed <= rewritten
        # colors/painting carriers are untouched
        assert before["Metadata/project_settings.config"] == after["Metadata/project_settings.config"]
        assert before["Metadata/slice_info.config"] == after["Metadata/slice_info.config"]
        assert before["Metadata/plate_1.png"] == after["Metadata/plate_1.png"]
        assert before["Auxiliaries/pic.webp"] == after["Auxiliaries/pic.webp"]


# ===========================================================================
# (d) each output then converts successfully via convert_single_file
# ===========================================================================
def test_each_output_converts_successfully(split_fixture):
    _, outputs, tmp_path = split_fixture
    for k, out in enumerate(outputs, start=1):
        converted = str(tmp_path / f"conv_{k}.3mf")
        ok, err = convert_single_file(out, converted, DEFAULT_COLORS)
        assert ok, f"plate {k}: {err}"
        with zipfile.ZipFile(converted) as z:
            assert "3D/3dmodel.model" in z.namelist()


# ===========================================================================
# (e) single-plate input refused with a clear message
# ===========================================================================
def test_single_plate_input_refused(tmp_path):
    src = str(tmp_path / "single.3mf")
    make_multi_plate_3mf(src, plates=1)
    ok, result = split_plates(src, str(tmp_path))
    assert ok is False
    assert "plate" in result.lower()
    assert "split" in result.lower()
    # nothing was written
    assert not [f for f in os.listdir(tmp_path) if "_plate" in f]


# ===========================================================================
# (f) unknown object_id in a plate -> loud failure, no outputs written
# ===========================================================================
def test_unknown_object_id_fails_loud(tmp_path):
    src = str(tmp_path / "bogus.3mf")
    make_multi_plate_3mf(src, plates=3, bogus_object_in_plate=2)
    ok, result = split_plates(src, str(tmp_path))
    assert ok is False
    assert "9999" in result
    assert not [f for f in os.listdir(tmp_path) if "_plate" in f]


def test_plate_without_instances_fails_loud(tmp_path):
    src = str(tmp_path / "emptyplate.3mf")
    make_multi_plate_3mf(src, plates=3, empty_plate=2)
    ok, result = split_plates(src, str(tmp_path))
    assert ok is False
    assert "plate 2" in result.lower()
    assert not [f for f in os.listdir(tmp_path) if "_plate" in f]


# ===========================================================================
# convert_or_split_plates: multi-plate -> ZIP of converted plates + saved to
# the settings output folder; single-plate -> a single .3mf.
# ===========================================================================
def test_convert_or_split_multiplate_one_file_and_saves(tmp_path):
    src = str(tmp_path / "multi.3mf")
    make_multi_plate_3mf(src, plates=3)
    out = str(tmp_path / "sess_U1_Ready.3mf")
    save_dir = str(tmp_path / "outfolder")

    ok, produced, msg = convert_or_split_plates(
        src, out, DEFAULT_COLORS, base_name="MyModel", save_dir=save_dir)

    assert ok, msg
    # Multi-plate is now kept in ONE file (Orca's plate grid), not a ZIP.
    assert produced.endswith(".3mf") and not produced.endswith(".zip")
    with zipfile.ZipFile(produced) as z:
        ms = z.read('Metadata/model_settings.config').decode('utf-8', 'ignore')
    assert ms.count('<plate>') == 3                     # all three plates in one file
    # a single print-ready file lands in the configured output folder
    assert os.listdir(save_dir) == ["MyModel_U1.3mf"]


def test_convert_or_split_singleplate_returns_3mf_and_saves(tmp_path):
    src = str(tmp_path / "single.3mf")
    make_multi_plate_3mf(src, plates=1)
    out = str(tmp_path / "sess_U1_Ready.3mf")
    save_dir = str(tmp_path / "outfolder")

    ok, produced, msg = convert_or_split_plates(
        src, out, DEFAULT_COLORS, base_name="Solo", save_dir=save_dir)

    assert ok, msg
    assert produced == out and produced.endswith(".3mf")
    assert os.listdir(save_dir) == ["Solo_U1.3mf"]


def test_reconvert_clears_stale_plate_outputs(tmp_path):
    """A re-conversion must clear this model's prior outputs (old per-plate files
    AND zip) and must not touch unrelated files in the output folder."""
    save_dir = tmp_path / "outfolder"
    save_dir.mkdir()
    # Simulate a previous (split-era) run: per-plate files + a zip, plus an
    # unrelated file and a different model's output that must both survive.
    for k in range(1, 6):
        (save_dir / f"MyModel_plate{k}_U1.3mf").write_bytes(b"stale")
    (save_dir / "MyModel_U1.zip").write_bytes(b"stale-zip")
    (save_dir / "Other_plate1_U1.3mf").write_bytes(b"keep")
    (save_dir / "notes.txt").write_bytes(b"keep")

    src = str(tmp_path / "multi.3mf")
    make_multi_plate_3mf(src, plates=3)
    out = str(tmp_path / "sess_U1_Ready.3mf")
    ok, produced, msg = convert_or_split_plates(
        src, out, DEFAULT_COLORS, base_name="MyModel", save_dir=str(save_dir))

    assert ok, msg
    names = sorted(os.listdir(save_dir))
    # Multi-plate now yields ONE file; all stale MyModel plate files + the zip
    # are gone; unrelated files untouched.
    assert names == ["MyModel_U1.3mf", "Other_plate1_U1.3mf", "notes.txt"]
    assert (save_dir / "MyModel_U1.3mf").read_bytes() != b"stale"


def test_compute_plate_cols_matches_orca():
    """Grid column count Orca uses (round(sqrt(n)), rounded up when sqrt>round).
    Verified against real Orca U1 files at N=2/5/6/8/10/16."""
    from app import compute_plate_cols
    expected = {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 3, 9: 3,
                10: 4, 12: 4, 16: 4}
    for n, cols in expected.items():
        assert compute_plate_cols(n) == cols, f"n={n}"


def test_multiplate_plates_land_on_distinct_grid_cells(tmp_path):
    """Each plate's items are recentered onto their own Orca grid cell, so
    consecutive plates in row 0 are offset by the X stride (bed.width * 1.2)."""
    import xml.etree.ElementTree as ET
    from app import (count_plates, compute_plate_cols, plate_cell_center,
                     parse_printable_area, _collect_build_items, make_zip_submodel_reader,
                     _MAIN_MODEL, _collect_global_corners, _parse_transform, _plate_object_ids)
    src = str(tmp_path / "multi.3mf")
    make_multi_plate_3mf(src, plates=3)
    out = str(tmp_path / "o.3mf")
    ok, produced, msg = convert_or_split_plates(src, out, DEFAULT_COLORS, base_name="G", save_dir=None)
    assert ok, msg
    with zipfile.ZipFile(produced) as z:
        ms = ET.fromstring(z.read('Metadata/model_settings.config').decode('utf-8', 'ignore'))
        import json as _json
        bed = parse_printable_area(_json.loads(z.read('Metadata/project_settings.config').decode('utf-8')))
        root = ET.fromstring(z.read('3D/3dmodel.model').decode('utf-8'))
        reader = make_zip_submodel_reader(z)
        getr = lambda p: root if p is _MAIN_MODEL else reader(p)
        items = {it.get('objectid'): it for it in _collect_build_items(root)}
        N = count_plates(ms); cols = compute_plate_cols(N)
        for k, plate in enumerate(ms.findall('.//plate'), start=1):
            box = [1e9] * 2 + [-1e9] * 2
            for oid in _plate_object_ids(plate):
                it = items.get(oid)
                if it is None:
                    continue
                A, t = _parse_transform(it.get('transform'), 'x'); c = []
                _collect_global_corners(_MAIN_MODEL, oid, A, t, getr, c)
                for p in c:
                    box[0] = min(box[0], p[0]); box[2] = max(box[2], p[0])
                    box[1] = min(box[1], p[1]); box[3] = max(box[3], p[1])
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            exp = plate_cell_center(k, cols, bed)
            assert abs(cx - exp[0]) < 1e-6 and abs(cy - exp[1]) < 1e-6, f"plate {k}"
