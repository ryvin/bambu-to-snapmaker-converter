#!/usr/bin/env python3
"""
Unit + regression tests for the group-recenter / bed-dims / per-item
drop-to-bed rewrite in app.py.

These are pure-Python tests (no Flask server, no browser). They build minimal
in-memory 3MF models and a synthetic single-plate .3mf fixture, plus exercise
the real 6-plate fixture at /mnt/e/Downloads/ButterflyWing_fans_U1.3mf when it
is present.

Run inside the container:
    docker exec bambu-to-u1-converter python -m pytest /app/test_recenter.py -v
"""
import os
import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

import app
from app import (
    ConversionError,
    BedBounds,
    parse_printable_area,
    recenter_and_drop_model,
    count_plates,
    convert_single_file,
    _collect_build_items,
    _collect_global_corners,
    _parse_transform,
    _index_from_dom,
    _stream_geom_index,
    make_zip_submodel_reader,
    _MAIN_MODEL,
    CORE_NS,
    PROD_NS,
)
import io

REAL_FIXTURE = "/mnt/e/Downloads/ButterflyWing_fans_U1.3mf"

# Bed derived from the actual template -> center (135.5, 136.0).
BED = BedBounds(0.5, 270.5, 1.0, 271.0)
BED_CX, BED_CY = 135.5, 136.0


# ---------------------------------------------------------------------------
# Helpers to synthesize minimal 3MF geometry
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


def build_main_model(objects, items):
    """
    objects: list of (id, aabb) -> object with a direct mesh.
    items: list of (objectid, transform_str_or_None).
    Returns a parsed ElementTree root (namespaced).
    """
    obj_xml = "".join(
        f'<object id="{oid}" type="model">{_mesh_xml(aabb)}</object>'
        for oid, aabb in objects
    )
    item_xml = ""
    for oid, tf in items:
        if tf is None:
            item_xml += f'<item objectid="{oid}"/>'
        else:
            item_xml += f'<item objectid="{oid}" transform="{tf}"/>'
    xml = (
        f'<model unit="millimeter" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}">'
        f"<resources>{obj_xml}</resources>"
        f"<build>{item_xml}</build>"
        f"</model>"
    )
    return ET.fromstring(xml)


def group_bbox_of_3mf(path):
    """Compute the global XY bbox + Z-min per item for every build item in a
    converted .3mf, resolving components through submodels (mirrors the app)."""
    with zipfile.ZipFile(path) as z:
        main = ET.fromstring(z.read("3D/3dmodel.model").decode("utf-8"))
        cache = {}

        def reader(p):
            name = str(p).lstrip("/")
            if name not in cache:
                cache[name] = (
                    ET.fromstring(z.read(name).decode("utf-8"))
                    if name in z.namelist()
                    else None
                )
            return cache[name]

        def get_root(p):
            return main if p is _MAIN_MODEL else reader(p)

        items = _collect_build_items(main)
        gminx = gminy = float("inf")
        gmaxx = gmaxy = float("-inf")
        item_minz = []
        for it in items:
            A, t = _parse_transform(it.get("transform"), "test")
            corners = []
            _collect_global_corners(_MAIN_MODEL, it.get("objectid"), A, t, get_root, corners)
            if not corners:
                continue
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            zs = [c[2] for c in corners]
            gminx, gmaxx = min(gminx, min(xs)), max(gmaxx, max(xs))
            gminy, gmaxy = min(gminy, min(ys)), max(gmaxy, max(ys))
            item_minz.append(min(zs))
    return gminx, gminy, gmaxx, gmaxy, item_minz


def _noreader(_path):
    return None


# ---------------------------------------------------------------------------
# Synthetic single-plate .3mf fixture (components -> submodels)
# ---------------------------------------------------------------------------
def _submodel_xml(object_id, aabb):
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}">'
        f'<resources><object id="{object_id}" type="model">'
        f"{_mesh_xml(aabb)}</object></resources><build/></model>"
    )


def make_single_plate_3mf(path, plates=1, empty_slice_info=False,
                          no_slice_info=False,
                          printer="Bambu Lab X1 Carbon",
                          extra_project_settings=None):
    """
    Build a minimal valid single-plate Bambu-style .3mf that uses
    components -> submodels (like real BambuStudio output). Two objects:
      obj 100 -> submodel a.model cube(0..10) lifted z+5, item at (300,300,2)
      obj 200 -> submodel b.model cube(0..20) identity, item at (350,300,2)
    model_settings carries a part matrix with z=41.99 to prove relative Z
    is preserved. `plates` controls how many <plate> blocks (for refuse test).

    ``empty_slice_info`` emits a slice_info.config with a header but NO
    <filament> nodes (as many real Bambu exports do). Filaments then come only
    from project_settings.config, exercising the id_mapping decoupling: the
    extruder remap must still work rather than silently no-op.
    """
    main = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}" '
        f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
        f'requiredextensions="p">'
        f"<resources>"
        f'<object id="100" type="model"><components>'
        f'<component p:path="/3D/Objects/a.model" objectid="1" '
        f'transform="1 0 0 0 1 0 0 0 1 0 0 5"/>'
        f"</components></object>"
        f'<object id="200" type="model"><components>'
        f'<component p:path="/3D/Objects/b.model" objectid="1" '
        f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        f"</components></object>"
        f"</resources>"
        f"<build>"
        f'<item objectid="100" transform="1 0 0 0 1 0 0 0 1 300 300 2" printable="1"/>'
        f'<item objectid="200" transform="1 0 0 0 1 0 0 0 1 350 300 2" printable="1"/>'
        f"</build></model>"
    )

    plate_blocks = ""
    for pid in range(1, plates + 1):
        plate_blocks += (
            f"<plate>"
            f'<metadata key="plater_id" value="{pid}"/>'
            f'<model_instance><metadata key="object_id" value="100"/>'
            f'<metadata key="instance_id" value="0"/></model_instance>'
            f"</plate>"
        )
    model_settings = (
        f'<?xml version="1.0" encoding="UTF-8"?><config>'
        f'<object id="100">'
        f'<metadata key="name" value="obj a"/>'
        f'<metadata key="extruder" value="1"/>'
        f'<part id="1" subtype="normal_part">'
        f'<metadata key="name" value="button"/>'
        f'<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 41.991806 0 0 0 1"/>'
        f"</part></object>"
        f'<object id="200">'
        f'<metadata key="name" value="obj b"/>'
        f'<metadata key="extruder" value="2"/>'
        f'<part id="1" subtype="normal_part">'
        f'<metadata key="name" value="body"/>'
        f'<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>'
        f"</part></object>"
        f"{plate_blocks}</config>"
    )

    if empty_slice_info:
        # Header-only slice_info, like many real Bambu exports: filaments live
        # only in project_settings.config.
        slice_info = (
            f'<?xml version="1.0" encoding="UTF-8"?><config>'
            f'<header><header_item key="X-BBL-Client-Type" value="slicer"/></header>'
            f"</config>"
        )
    else:
        slice_info = (
            f'<?xml version="1.0" encoding="UTF-8"?><config>'
            f'<header><header_item key="X-BBL-Client-Type" value="slicer"/></header>'
            f'<plate>'
            f'<metadata key="index" value="1"/>'
            f'<metadata key="printer_model_id" value="Bambu Lab X1 Carbon"/>'
            f'<filament id="1" tray_info_idx="GFA00" type="PLA" color="#FF0000" used_m="1" used_g="1"/>'
            f'<filament id="2" tray_info_idx="GFA01" type="PLA" color="#00FF00" used_m="1" used_g="1"/>'
            f"</plate></config>"
        )

    _ps = {
        "printer_model": printer,
        "printer_settings_id": printer,
        "different_settings_to_system": [],
        "filament_colour": ["#FF0000", "#00FF00"],
        "filament_type": ["PLA", "PLA"],
    }
    if extra_project_settings:
        _ps.update(extra_project_settings)
    project_settings = json.dumps(_ps)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/Objects/a.model", _submodel_xml(1, (0, 0, 0, 10, 10, 10)))
        z.writestr("3D/Objects/b.model", _submodel_xml(1, (0, 0, 0, 20, 20, 20)))
        z.writestr("Metadata/model_settings.config", model_settings)
        if not no_slice_info:
            z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/project_settings.config", project_settings)


DEFAULT_COLORS = {
    "1": {"color": "#FF0000FF", "type": "PLA"},
    "2": {"color": "#00FF00FF", "type": "PLA"},
}


# ===========================================================================
# Test 1: single-object bbox center == bed center
# ===========================================================================
def test_single_object_centered_on_bed():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10))],
        items=[("1", "1 0 0 0 1 0 0 0 1 200 50 0")],
    )
    recenter_and_drop_model(root, _noreader, BED)
    item = _collect_build_items(root)[0]
    A, t = _parse_transform(item.get("transform"), "t")
    # cube spans 10mm; its center after translation must land on bed center.
    assert t[0] + 5 == pytest.approx(BED_CX, abs=1e-6)
    assert t[1] + 5 == pytest.approx(BED_CY, abs=1e-6)


# ===========================================================================
# Test 2: multi-object -> relative offsets preserved + group centered
# ===========================================================================
def test_multi_object_preserves_relative_offsets():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10)), ("2", (0, 0, 0, 10, 10, 10))],
        items=[
            ("1", "1 0 0 0 1 0 0 0 1 300 300 0"),
            ("2", "1 0 0 0 1 0 0 0 1 360 300 0"),
        ],
    )
    items = _collect_build_items(root)
    before = [_parse_transform(i.get("transform"), "t")[1] for i in items]
    recenter_and_drop_model(root, _noreader, BED)
    after = [_parse_transform(i.get("transform"), "t")[1] for i in items]

    # Every pairwise offset is preserved exactly.
    assert after[1][0] - after[0][0] == pytest.approx(before[1][0] - before[0][0], abs=1e-9)
    assert after[1][1] - after[0][1] == pytest.approx(before[1][1] - before[0][1], abs=1e-9)

    # Group bbox center lands on the bed center.
    xs = [after[0][0], after[0][0] + 10, after[1][0], after[1][0] + 10]
    ys = [after[0][1], after[0][1] + 10]
    assert (min(xs) + max(xs)) / 2 == pytest.approx(BED_CX, abs=1e-6)
    assert (min(ys) + max(ys)) / 2 == pytest.approx(BED_CY, abs=1e-6)


# ===========================================================================
# Test 3: delta applied exactly once (dedup across findall passes)
# ===========================================================================
def test_delta_applied_exactly_once():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10)), ("2", (0, 0, 0, 10, 10, 10))],
        items=[
            ("1", "1 0 0 0 1 0 0 0 1 300 300 0"),
            ("2", "1 0 0 0 1 0 0 0 1 360 300 0"),
        ],
    )
    # Namespaced items must be enumerated once, not twice.
    assert len(_collect_build_items(root)) == 2
    recenter_and_drop_model(root, _noreader, BED)
    # A double-shift would overshoot the bed center; verify it did not.
    after = [_parse_transform(i.get("transform"), "t")[1] for i in _collect_build_items(root)]
    xs = [after[0][0], after[0][0] + 10, after[1][0], after[1][0] + 10]
    assert (min(xs) + max(xs)) / 2 == pytest.approx(BED_CX, abs=1e-6)


# ===========================================================================
# Test 4: missing transform attr -> treated as identity, still written
# ===========================================================================
def test_missing_transform_treated_as_identity():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10))],
        items=[("1", None)],  # no transform attribute at all
    )
    recenter_and_drop_model(root, _noreader, BED)
    item = _collect_build_items(root)[0]
    tf = item.get("transform")
    assert tf is not None  # identity+delta was written
    A, t = _parse_transform(tf, "t")
    assert A == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    assert t[0] + 5 == pytest.approx(BED_CX, abs=1e-6)
    assert t[1] + 5 == pytest.approx(BED_CY, abs=1e-6)
    # Within printable bounds.
    assert BED.min_x <= t[0] and t[0] + 10 <= BED.max_x
    assert BED.min_y <= t[1] and t[1] + 10 <= BED.max_y


# ===========================================================================
# Test 5: malformed transform -> ConversionError naming the offending token
# ===========================================================================
def test_malformed_transform_wrong_count_raises():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10))],
        items=[("1", "1 0 0 0 1 0 0 0 1 200 50")],  # 11 values
    )
    with pytest.raises(ConversionError) as ei:
        recenter_and_drop_model(root, _noreader, BED)
    assert "11" in str(ei.value)


def test_malformed_transform_non_numeric_raises():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 10, 10, 10))],
        items=[("1", "1 0 0 0 1 0 0 0 1 200 NaNaN 0")],
    )
    with pytest.raises(ConversionError) as ei:
        recenter_and_drop_model(root, _noreader, BED)
    assert "NaNaN" in str(ei.value)  # offending token surfaced


# ===========================================================================
# Test 6: group larger than printable -> ConversionError with dims
# ===========================================================================
def test_oversize_group_raises_fit_error():
    root = build_main_model(
        objects=[("1", (0, 0, 0, 400, 10, 10))],  # 400mm wide, bed is 270
        items=[("1", "1 0 0 0 1 0 0 0 1 0 0 0")],
    )
    with pytest.raises(ConversionError) as ei:
        recenter_and_drop_model(root, _noreader, BED)
    msg = str(ei.value)
    assert "400" in msg and "270" in msg


# ===========================================================================
# Test 7: per-item drop-to-bed; part relative Z is preserved (not zeroed)
# ===========================================================================
def test_part_relative_z_preserved_and_item_dropped(tmp_path):
    src = str(tmp_path / "in.3mf")
    out = str(tmp_path / "out.3mf")
    make_single_plate_3mf(src, plates=1)

    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err

    # (a) The part matrix Z (41.99) survives — no blanket index-11 zeroing.
    with zipfile.ZipFile(out) as z:
        ms = z.read("Metadata/model_settings.config").decode("utf-8")
    assert "41.991806" in ms

    # (b) Each item is dropped to the bed: world min-Z == 0.
    _, _, _, _, item_minz = group_bbox_of_3mf(out)
    for mz in item_minz:
        assert mz == pytest.approx(0.0, abs=1e-6)


# ===========================================================================
# Test 8: multi-plate -> refuse with a clear error (policy iii)
# ===========================================================================
def test_multiplate_refused_with_clear_error(tmp_path):
    # merge_plates=False is the legacy split path's contract: it still refuses a
    # multi-plate file (the split feeds it one plate at a time). merge_plates=True
    # (default) keeps all plates in one file instead — covered in test_split.py.
    src = str(tmp_path / "multi.3mf")
    out = str(tmp_path / "multi_out.3mf")
    make_single_plate_3mf(src, plates=3)

    ok, err = convert_single_file(src, out, DEFAULT_COLORS, merge_plates=False)
    assert ok is False
    assert "3" in err  # plate count surfaced
    assert "plate" in err.lower()


def test_real_fixture_refused_as_multiplate(tmp_path):
    if not os.path.exists(REAL_FIXTURE):
        pytest.skip("real 6-plate fixture not present")
    out = str(tmp_path / "real_out.3mf")
    colors = {"1": {"color": "#FF0000FF", "type": "PLA"}}
    ok, err = convert_single_file(REAL_FIXTURE, out, colors, merge_plates=False)
    assert ok is False
    # The real file's plate count can change between sessions; assert the
    # multi-plate refusal shape (split path), not a hard-coded count.
    import re as _re
    assert _re.search(r"contains \d+ plates", err)
    assert "plate" in err.lower()


# ===========================================================================
# Test 9: full regression through convert_single_file on synthetic fixture
# ===========================================================================
def test_full_conversion_places_group_on_bed(tmp_path):
    src = str(tmp_path / "virgin.3mf")
    out = str(tmp_path / "virgin_out.3mf")
    make_single_plate_3mf(src, plates=1)

    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err

    # Every output item transform parses as 12 floats.
    with zipfile.ZipFile(out) as z:
        main = ET.fromstring(z.read("3D/3dmodel.model").decode("utf-8"))
    for it in _collect_build_items(main):
        A, t = _parse_transform(it.get("transform"), "t")  # raises if malformed
        assert len(A) == 9 and len(t) == 3

    # Group bbox is a subset of the printable area and centered on the bed.
    gminx, gminy, gmaxx, gmaxy, _ = group_bbox_of_3mf(out)
    assert gminx >= BED.min_x - 1e-6
    assert gmaxx <= BED.max_x + 1e-6
    assert gminy >= BED.min_y - 1e-6
    assert gmaxy <= BED.max_y + 1e-6
    assert (gminx + gmaxx) / 2 == pytest.approx(BED_CX, abs=1e-6)
    assert (gminy + gmaxy) / 2 == pytest.approx(BED_CY, abs=1e-6)


# ===========================================================================
# Test 10: bed bounds derived from template (guards constant reintroduction)
# ===========================================================================
def test_bed_bounds_from_template():
    with zipfile.ZipFile("u1_template.3mf") as z:
        ps = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    bed = parse_printable_area(ps)
    assert (bed.min_x, bed.max_x, bed.min_y, bed.max_y) == (0.5, 270.5, 1.0, 271.0)
    assert bed.center_x == pytest.approx(135.5)
    assert bed.center_y == pytest.approx(136.0)
    # The old hard-coded 230/115 fiction must be gone.
    assert not hasattr(app, "U1_BED_SIZE")
    assert not hasattr(app, "U1_BED_CENTER")


def test_missing_printable_area_raises():
    with pytest.raises(ConversionError):
        parse_printable_area({})


# ===========================================================================
# A .3mf with NO slice_info.config at all (common for non-Bambu exports) must
# still convert; the output gains a synthesized slice_info entry.
# ===========================================================================
def test_missing_sliceinfo_is_synthesized(tmp_path):
    src = str(tmp_path / "nosi.3mf")
    out = str(tmp_path / "nosi_out.3mf")
    make_single_plate_3mf(src, plates=1, no_slice_info=True)

    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err
    with zipfile.ZipFile(out) as z:
        assert "Metadata/slice_info.config" in z.namelist()


# ===========================================================================
# Test 11: empty slice_info still builds id_mapping (extruders remapped)
# Regression: real Bambu exports often ship a header-only slice_info.config;
# tying id_mapping to a slice_info <filament> lookup left it empty, so painted
# regions were never remapped and the new fail-loud guard would falsely reject.
# ===========================================================================
def test_empty_sliceinfo_still_remaps_extruders(tmp_path):
    src = str(tmp_path / "empty_si.3mf")
    out = str(tmp_path / "empty_si_out.3mf")
    make_single_plate_3mf(src, plates=1, empty_slice_info=True)

    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err  # must NOT falsely raise "no filament mapping"

    with zipfile.ZipFile(out) as z:
        ms = ET.fromstring(z.read("Metadata/model_settings.config").decode("utf-8"))
    refs = {m.get("value") for m in ms.findall('.//metadata[@key="extruder"]')}
    # Every painted-region extruder points at a kept sequential filament.
    assert refs and refs <= {"1", "2"}


# ===========================================================================
# Test 12: a painted region with no selected filament still fails loudly
# (guards that decoupling id_mapping did not disable the fail-loud guard).
# ===========================================================================
def test_uncovered_painted_region_raises(tmp_path):
    src = str(tmp_path / "uncovered.3mf")
    out = str(tmp_path / "uncovered_out.3mf")
    make_single_plate_3mf(src, plates=1, empty_slice_info=True)

    # Keep only filament "1"; object 200 references extruder "2" -> uncovered.
    colors = {"1": {"color": "#FF0000FF", "type": "PLA"}}
    ok, err = convert_single_file(src, out, colors)
    assert ok is False
    assert "2" in err and "extruder" in err.lower()


# ===========================================================================
# Test 13: geometry-only re-fix keeps every non-model entry byte-identical
# (colors/painting untouched) while recentering the model on the bed.
# ===========================================================================
def test_refix_preserves_all_configs_and_recenters(tmp_path):
    import hashlib
    from refix import refix_geometry_only

    src = str(tmp_path / "keepcolors.3mf")
    out = str(tmp_path / "keepcolors_fixed.3mf")
    make_single_plate_3mf(src, plates=1, printer="Snapmaker U1 (0.4 nozzle)")

    ok, err = refix_geometry_only(src, out)
    assert ok, err

    def entry_hashes(path):
        with zipfile.ZipFile(path) as z:
            return {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist()}

    before, after = entry_hashes(src), entry_hashes(out)
    # Same set of entries; ONLY the 3D model changed.
    assert set(before) == set(after)
    changed = [n for n in before if before[n] != after[n]]
    assert changed == ["3D/3dmodel.model"]

    # Colors/types config is byte-for-byte identical.
    with zipfile.ZipFile(src) as z:
        ps_before = z.read("Metadata/project_settings.config")
    with zipfile.ZipFile(out) as z:
        ps_after = z.read("Metadata/project_settings.config")
    assert ps_before == ps_after

    # Model is now centered on the (template-derived) bed and dropped to z=0.
    gminx, gminy, gmaxx, gmaxy, item_minz = group_bbox_of_3mf(out)
    assert (gminx + gmaxx) / 2 == pytest.approx(BED_CX, abs=1e-6)
    assert (gminy + gmaxy) / 2 == pytest.approx(BED_CY, abs=1e-6)
    for mz in item_minz:
        assert mz == pytest.approx(0.0, abs=1e-6)


# ===========================================================================
# Test 14: geometry-only re-fix refuses multi-plate (same policy)
# ===========================================================================
def test_refix_refuses_multiplate(tmp_path):
    from refix import refix_geometry_only

    src = str(tmp_path / "multi.3mf")
    out = str(tmp_path / "multi_fixed.3mf")
    make_single_plate_3mf(src, plates=3)

    ok, err = refix_geometry_only(src, out)
    assert ok is False
    assert "3" in err and "plate" in err.lower()
    assert not os.path.exists(out)


# ===========================================================================
# Test: refix refuses a file still on a Bambu profile (needs full re-convert).
# Geometry-only re-fix cannot swap the printer profile, so a mis-converted file
# must not be handed back looking "fixed".
# ===========================================================================
def test_refix_refuses_non_u1_profile(tmp_path):
    from refix import refix_geometry_only

    src = str(tmp_path / "still_bambu.3mf")
    out = str(tmp_path / "still_bambu_fixed.3mf")
    make_single_plate_3mf(src, plates=1, printer="Bambu Lab A1 0.2 nozzle")

    ok, err = refix_geometry_only(src, out)
    assert ok is False
    assert "not a Snapmaker U1" in err
    assert not os.path.exists(out)


# ===========================================================================
# Test 15: geometry resolves when a submodel OMITS the core namespace.
# Real Bambu MeshGraffiti/MakerLab exports write submodels whose <object>/
# <mesh>/<vertex> are in no namespace; strict {CORE_NS} matching found nothing
# and refused the whole model as "no resolvable geometry".
# ===========================================================================
def test_geometry_resolves_without_core_namespace():
    main = ET.fromstring(
        f'<model xmlns="{CORE_NS}" xmlns:p="{PROD_NS}">'
        f'<resources><object id="1" type="model"><components>'
        f'<component p:path="/sub.model" objectid="9"/>'
        f'</components></object></resources>'
        f'<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>'
        f'</model>'
    )
    # Submodel WITHOUT a default core namespace: elements are un-namespaced.
    sub = ET.fromstring(
        f'<model xmlns:p="{PROD_NS}"><resources>'
        f'<object id="9" type="model"><mesh><vertices>'
        f'<vertex x="0" y="0" z="0"/><vertex x="10" y="20" z="4"/>'
        f'</vertices></mesh></object></resources></model>'
    )

    def get_root(path):
        if path is _MAIN_MODEL:
            return main
        return sub if str(path).lstrip('/') == 'sub.model' else None

    corners = []
    _collect_global_corners(
        _MAIN_MODEL, "1", [1, 0, 0, 0, 1, 0, 0, 0, 1], [0, 0, 0], get_root, corners
    )
    assert corners, "geometry must resolve despite the missing core namespace"
    xs = [c[0] for c in corners]; ys = [c[1] for c in corners]; zs = [c[2] for c in corners]
    assert (min(xs), max(xs)) == (0, 10)
    assert (min(ys), max(ys)) == (0, 20)
    assert (min(zs), max(zs)) == (0, 4)


# --- Streaming geometry-index regression (perf refactor: parse the 100+ MB mesh
#     once via iterparse instead of a full DOM + re-walks). The streaming index
#     must be byte-for-byte equal to the DOM index it replaces. ---

_PARITY_MODELS = [
    # (label, xml) — namespaced + un-namespaced, meshes, components w/ p:path.
    (
        "namespaced-mesh-and-components",
        f'<model xmlns="{CORE_NS}" xmlns:p="{PROD_NS}"><resources>'
        f'<object id="1" type="model"><mesh><vertices>'
        f'<vertex x="-3" y="1" z="0.5"/><vertex x="4" y="9" z="2"/>'
        f'<vertex x="0" y="-2" z="-1"/></vertices>'
        f'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
        f'<object id="2" type="model"><components>'
        f'<component objectid="1" transform="1 0 0 0 1 0 0 0 1 5 6 7" p:path="/3D/Objects/sub.model"/>'
        f'</components></object>'
        f'</resources><build><item objectid="2"/></build></model>',
    ),
    (
        "no-core-namespace",
        f'<model xmlns:p="{PROD_NS}"><resources>'
        f'<object id="7"><mesh><vertices>'
        f'<vertex x="0" y="0" z="0"/><vertex x="10" y="20" z="4"/>'
        f'</vertices></mesh></object></resources></model>',
    ),
    (
        "object-no-vertices-only-components",
        f'<model xmlns="{CORE_NS}"><resources>'
        f'<object id="5"><components>'
        f'<component objectid="1" transform="2 0 0 0 2 0 0 0 2 0 0 0"/>'
        f'</components></object></resources></model>',
    ),
    (
        "bad-vertex-coord-skipped",
        f'<model xmlns="{CORE_NS}"><resources>'
        f'<object id="3"><mesh><vertices>'
        f'<vertex x="1" y="2" z="3"/><vertex x="oops" y="2" z="3"/>'
        f'<vertex x="5" y="1" z="9"/></vertices></mesh></object></resources></model>',
    ),
    (
        "first-mesh-only",
        f'<model xmlns="{CORE_NS}"><resources>'
        f'<object id="4"><mesh><vertices><vertex x="0" y="0" z="0"/>'
        f'<vertex x="1" y="1" z="1"/></vertices></mesh>'
        f'<mesh><vertices><vertex x="99" y="99" z="99"/></vertices></mesh>'
        f'</object></resources></model>',
    ),
]


@pytest.mark.parametrize("label,xml", _PARITY_MODELS, ids=[m[0] for m in _PARITY_MODELS])
def test_stream_index_matches_dom_index(label, xml):
    dom = _index_from_dom(ET.fromstring(xml))
    stream = _stream_geom_index(io.BytesIO(xml.encode("utf-8")))
    assert stream.objects == dom.objects, f"stream/DOM index diverged for {label}"


def test_stream_index_prune_stress(monkeypatch):
    """Many vertices force repeated child-pruning; AABB must still match DOM."""
    monkeypatch.setattr(app, "_PRUNE_EVERY", 3)  # force many prunes on a tiny input
    verts = "".join(
        f'<vertex x="{i}" y="{-i}" z="{i * 0.5}"/>' for i in range(50)
    )
    tris = "".join(f'<triangle v1="0" v2="1" v3="2"/>' for _ in range(50))
    xml = (
        f'<model xmlns="{CORE_NS}"><resources><object id="1"><mesh>'
        f'<vertices>{verts}</vertices><triangles>{tris}</triangles>'
        f'</mesh></object></resources></model>'
    )
    dom = _index_from_dom(ET.fromstring(xml))
    stream = _stream_geom_index(io.BytesIO(xml.encode("utf-8")))
    assert stream.objects == dom.objects
    assert stream.objects["1"]["aabb"] == (0.0, -49.0, 0.0, 49.0, 0.0, 24.5)


def test_zip_submodel_reader_corrupt_message(tmp_path):
    p = tmp_path / "broken.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/Objects/object_1.model", "<model><resources><object")  # truncated
    with zipfile.ZipFile(p) as zin:
        read = make_zip_submodel_reader(zin)
        with pytest.raises(ConversionError) as ei:
            read("/3D/Objects/object_1.model")
    assert "Corrupt submodel '3D/Objects/object_1.model'" in str(ei.value)


def test_zip_submodel_reader_missing_returns_none(tmp_path):
    p = tmp_path / "empty.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/x.config", "x")
    with zipfile.ZipFile(p) as zin:
        read = make_zip_submodel_reader(zin)
        assert read("/3D/Objects/nope.model") is None


def test_source_layer_height_is_preserved_not_template(tmp_path):
    """The source's layer grid must survive conversion. Forcing the U1 template's
    coarser 0.2mm (the old behaviour) halves a fine lithophane's layers and
    knocks custom_gcode_per_layer.xml by-height color changes off their layer
    boundaries so they stop firing."""
    src = str(tmp_path / "fine.3mf")
    make_single_plate_3mf(src, extra_project_settings={
        "layer_height": "0.08", "initial_layer_print_height": "0.16"})
    out = str(tmp_path / "fine_U1.3mf")
    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err
    with zipfile.ZipFile(out) as z:
        ps = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert ps["layer_height"] == "0.08"                 # source value, not template's 0.2
    assert ps["initial_layer_print_height"] == "0.16"


def test_source_without_layer_height_uses_template(tmp_path):
    """When the source omits layer settings, the template's value stands (no crash)."""
    src = str(tmp_path / "plain.3mf")
    make_single_plate_3mf(src)                            # no layer_height in project_settings
    out = str(tmp_path / "plain_U1.3mf")
    ok, err = convert_single_file(src, out, DEFAULT_COLORS)
    assert ok, err
    with zipfile.ZipFile(out) as z:
        ps = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert ps["layer_height"] == "0.2"                   # template default preserved
