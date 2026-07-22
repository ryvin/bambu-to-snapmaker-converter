import os
import zipfile
import shutil
import re
import json
import uuid
import time
import math
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from flask import Flask, render_template, request, send_file, jsonify
from history import HistoryManager


class ConversionError(Exception):
    """Raised for any recoverable failure during .3mf conversion.

    convert_single_file catches this at its boundary and returns
    (False, message) instead of letting the exception escape, but the
    helpers raise loudly so failures are never silently swallowed.
    """
    pass

app = Flask(__name__)

# Initialize history manager (use data directory for Docker persistence)
DATA_DIR = os.environ.get('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)
history_manager = HistoryManager(os.path.join(DATA_DIR, 'conversion_history.json'))

# CONFIGURATION
UPLOAD_FOLDER = 'uploads'
TEMPLATE_FILE = 'u1_template.3mf'  # Your empty U1 .3mf file
FILAMENT_PROFILES_FILE = 'filament_types.3mf'  # Reference file with available filament profiles
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure folders exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Cleanup settings
MAX_FILE_AGE_HOURS = 8

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE_HOURS from uploads folder."""
    now = time.time()
    max_age_seconds = MAX_FILE_AGE_HOURS * 3600

    try:
        for filename in os.listdir(UPLOAD_FOLDER):
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                file_age = now - os.path.getmtime(filepath)
                if file_age > max_age_seconds:
                    os.remove(filepath)
    except Exception as e:
        print(f"Cleanup error: {e}")

# Load available filament profiles from filament_types.3mf
AVAILABLE_FILAMENTS = []
try:
    with zipfile.ZipFile(FILAMENT_PROFILES_FILE, 'r') as z:
        settings = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
        types = settings.get('filament_type', [])
        ids = settings.get('filament_settings_id', [])
        for t, sid in zip(types, ids):
            AVAILABLE_FILAMENTS.append({
                'type': t,
                'settings_id': sid
            })
    print(f"Loaded {len(AVAILABLE_FILAMENTS)} filament profiles from {FILAMENT_PROFILES_FILE}")
except Exception as e:
    print(f"Warning: Could not load filament profiles: {e}")
    # Fallback defaults
    AVAILABLE_FILAMENTS = [
        {'type': 'PLA', 'settings_id': 'Snapmaker PLA SnapSpeed @U1'},
        {'type': 'PETG', 'settings_id': 'Snapmaker PETG HF'},
        {'type': 'ABS', 'settings_id': 'Generic ABS'},
        {'type': 'TPU', 'settings_id': 'Generic TPU'},
    ]

def is_bambu_file(filepath):
    """
    Check if a .3mf file is a Bambu Lab file (not already Snapmaker).
    Returns True if it's a Bambu file, False if Snapmaker or unrecognized.
    """
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            if "Metadata/slice_info.config" in z.namelist():
                with z.open("Metadata/slice_info.config") as f:
                    content = f.read().decode('utf-8')
                    # Check for Bambu printer models
                    if 'Snapmaker' in content:
                        return False
                    if 'Bambu' in content or 'BambuLab' in content:
                        return True
            # If no slice_info, check project_settings
            if "Metadata/project_settings.config" in z.namelist():
                with z.open("Metadata/project_settings.config") as f:
                    settings = json.loads(f.read().decode('utf-8'))
                    printer = settings.get('printer_model', '')
                    if 'Snapmaker' in printer:
                        return False
                    if 'Bambu' in printer or 'X1' in printer or 'P1' in printer or 'A1' in printer:
                        return True
            return True  # Default to True if can't determine
    except Exception as e:
        print(f"Error checking file type: {e}")
        return False


def auto_map_filaments(filaments):
    """
    Automatically map filament types to closest U1 profiles.
    Returns a colors dict ready for conversion.
    """
    # Build type mapping from available filaments
    type_mapping = {}
    for ft in AVAILABLE_FILAMENTS:
        base_type = ft['type'].upper().replace('-HF', '').replace('-', '')
        type_mapping[base_type] = ft['type']

    colors = {}
    for fil in filaments:
        original_type = (fil.get('type') or 'PLA').upper()
        # Try to find matching U1 type
        mapped_type = None
        for base, u1_type in type_mapping.items():
            if base in original_type or original_type in base:
                mapped_type = u1_type
                break
        # Default to PLA if no match
        if not mapped_type:
            mapped_type = AVAILABLE_FILAMENTS[0]['type'] if AVAILABLE_FILAMENTS else 'PLA'

        colors[fil['id']] = {
            'color': fil['color'],
            'type': mapped_type
        }
    return colors


def normalize_color(color):
    """
    Normalize color to #RRGGBB format for HTML color input compatibility.
    Handles colors with or without #, and with alpha channel (8 chars).
    """
    if not color:
        return "#000000"
    # Remove # if present
    color = color.lstrip('#')
    # If 8 characters (with alpha), take only first 6 (RGB)
    if len(color) == 8:
        color = color[:6]
    # Ensure 6 characters
    if len(color) != 6:
        return "#000000"
    return f"#{color.upper()}"

def parse_bambu_filaments(filepath):
    """
    Opens the 3MF and returns a list of current filaments/colors.
    First tries slice_info.config, then falls back to project_settings.config.
    """
    filaments = []
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            # First try slice_info.config
            if "Metadata/slice_info.config" in z.namelist():
                with z.open("Metadata/slice_info.config") as f:
                    xml_content = f.read().decode('utf-8')
                    root = ET.fromstring(xml_content)
                    for fil in root.findall(".//filament"):
                        f_id = fil.get('id')
                        f_color = normalize_color(fil.get('color'))
                        f_type = fil.get('type') or 'PLA'
                        filaments.append({
                            'id': f_id,
                            'color': f_color,
                            'type': f_type
                        })

            # If no filaments found in slice_info, try project_settings.config
            if not filaments and "Metadata/project_settings.config" in z.namelist():
                with z.open("Metadata/project_settings.config") as f:
                    settings = json.loads(f.read().decode('utf-8'))
                    colors = settings.get('filament_colour', [])
                    types = settings.get('filament_type', [])

                    for i, color in enumerate(colors):
                        f_type = types[i] if i < len(types) else 'PLA'
                        filaments.append({
                            'id': str(i + 1),  # 1-based IDs
                            'color': normalize_color(color),
                            'type': f_type
                        })

    except zipfile.BadZipFile as e:
        raise ConversionError(
            f"Cannot read 3MF archive '{os.path.basename(filepath)}': {e}"
        )
    except (ET.ParseError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConversionError(
            f"Corrupt filament metadata in '{os.path.basename(filepath)}': {e}"
        )
    return filaments


# ---------------------------------------------------------------------------
# Bed geometry (derived from the selected U1 template, never hard-coded)
# ---------------------------------------------------------------------------
# 3MF core + production namespaces used in 3dmodel.model and submodels.
CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD_NS = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'

# Sentinel meaning "the main 3dmodel.model" when resolving component paths.
_MAIN_MODEL = object()

# If a build item resolves to no geometry at all we can still keep it in the
# group layout, but we cannot drop it to the bed. Warned, not fatal (unless
# NO item anywhere has geometry).
_Z_DROP_EPS = 1e-6


@dataclass(frozen=True)
class BedBounds:
    """Printable-area bounds parsed from a template's printable_area polygon."""
    min_x: float
    max_x: float
    min_y: float
    max_y: float

    @property
    def center_x(self):
        return (self.min_x + self.max_x) / 2.0

    @property
    def center_y(self):
        return (self.min_y + self.max_y) / 2.0

    @property
    def width(self):
        return self.max_x - self.min_x

    @property
    def height(self):
        return self.max_y - self.min_y


def parse_printable_area(project_settings):
    """
    Parse the ``printable_area`` polygon from a project_settings dict into
    independent X/Y bounds. The polygon is a list of "XxY" strings, e.g.
    ['0.5x1', '270.5x1', '270.5x271', '0.5x271'].

    Raises ConversionError if the key is missing or malformed.
    """
    poly = project_settings.get('printable_area')
    if not poly:
        raise ConversionError(
            "Template project_settings.config is missing 'printable_area'; "
            "cannot determine U1 bed bounds."
        )
    xs, ys = [], []
    for pt in poly:
        try:
            sx, sy = str(pt).split('x')
            xs.append(float(sx))
            ys.append(float(sy))
        except (ValueError, AttributeError):
            raise ConversionError(
                f"Malformed printable_area point '{pt}' in template "
                f"(expected 'XxY')."
            )
    return BedBounds(min(xs), max(xs), min(ys), max(ys))


def _parse_transform(transform_str, label):
    """
    Parse a 12-value 3MF transform string into (A, t) where A is the 9-value
    row-major 3x3 linear part and t is [tx, ty, tz]. A point maps as
    p' = p . A + t (row-vector convention).

    A missing/None transform is the 3MF identity. Any other malformation
    (wrong count, non-numeric token) raises ConversionError naming the
    offending object and token.
    """
    if transform_str is None:
        return ([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0])
    parts = transform_str.split()
    if len(parts) != 12:
        raise ConversionError(
            f"Transform for {label} has {len(parts)} values (expected 12): "
            f"'{transform_str}'"
        )
    values = []
    for tok in parts:
        try:
            values.append(float(tok))
        except ValueError:
            raise ConversionError(
                f"Transform for {label} contains non-numeric token '{tok}' "
                f"in '{transform_str}'"
            )
    return (values[0:9], values[9:12])


def _apply(A, t, p):
    """Apply p' = p . A + t (row-vector, A row-major 3x3)."""
    x, y, z = p
    return (
        x * A[0] + y * A[3] + z * A[6] + t[0],
        x * A[1] + y * A[4] + z * A[7] + t[1],
        x * A[2] + y * A[5] + z * A[8] + t[2],
    )


def _matmul3(A, B):
    """Row-major 3x3 multiply so that (p.A).B == p.(A.B)."""
    R = [0.0] * 9
    for i in range(3):
        for j in range(3):
            R[i * 3 + j] = (
                A[i * 3 + 0] * B[0 * 3 + j]
                + A[i * 3 + 1] * B[1 * 3 + j]
                + A[i * 3 + 2] * B[2 * 3 + j]
            )
    return R


def _lname(tag):
    """Local element/attribute name, namespace-agnostic ('{ns}object' -> 'object').

    Bambu submodels frequently OMIT the default core-namespace declaration, so
    their <object>/<mesh>/<vertex> elements end up in no namespace. Matching on
    the local name resolves geometry whether or not the core namespace is
    declared, instead of silently finding nothing.
    """
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else tag


def _find_child(elem, local):
    """First direct child whose local name matches, ignoring namespace."""
    for c in elem:
        if _lname(c.tag) == local:
            return c
    return None


def _get_attr(elem, local):
    """Attribute value by local name, ignoring namespace (plain first)."""
    v = elem.get(local)
    if v is not None:
        return v
    for k, val in elem.attrib.items():
        if _lname(k) == local:
            return val
    return None


def _object_map(root):
    """Return {object_id: <object> element} for a parsed model/submodel root."""
    cache = getattr(root, '_u1_object_map', None)
    if cache is not None:
        return cache
    cache = {}
    for obj in root.iter():
        if _lname(obj.tag) != 'object':
            continue
        oid = obj.get('id')
        if oid is not None:
            cache[oid] = obj
    try:
        root._u1_object_map = cache
    except AttributeError:
        pass
    return cache


def _object_local_aabb(obj_elem):
    """
    Local axis-aligned bounding box of an object's own mesh, as
    (minx, miny, minz, maxx, maxy, maxz), or None if it has no vertices.
    """
    mesh = _find_child(obj_elem, 'mesh')
    if mesh is None:
        return None
    verts = _find_child(mesh, 'vertices')
    if verts is None:
        return None
    minx = miny = minz = math.inf
    maxx = maxy = maxz = -math.inf
    found = False
    for v in verts:
        if _lname(v.tag) != 'vertex':
            continue
        try:
            x = float(v.get('x'))
            y = float(v.get('y'))
            z = float(v.get('z'))
        except (TypeError, ValueError):
            continue
        found = True
        minx, maxx = min(minx, x), max(maxx, x)
        miny, maxy = min(miny, y), max(maxy, y)
        minz, maxz = min(minz, z), max(maxz, z)
    if not found:
        return None
    return (minx, miny, minz, maxx, maxy, maxz)


def _collect_global_corners(file_path, object_id, A, t, get_root, corners, depth=0):
    """
    Recursively accumulate global-space AABB corners for an object.

    (A, t) maps this object's local coordinates to global. Components are
    resolved through their own transform and referenced submodel file.
    """
    if depth > 64:
        raise ConversionError(
            f"Component nesting exceeded depth 64 (objectid={object_id}); "
            f"aborting to avoid a cycle."
        )
    root = get_root(file_path)
    if root is None:
        return  # missing submodel file -> contributes no geometry
    obj = _object_map(root).get(str(object_id))
    if obj is None:
        return

    aabb = _object_local_aabb(obj)
    if aabb is not None:
        minx, miny, minz, maxx, maxy, maxz = aabb
        for cx in (minx, maxx):
            for cy in (miny, maxy):
                for cz in (minz, maxz):
                    corners.append(_apply(A, t, (cx, cy, cz)))

    comps = _find_child(obj, 'components')
    if comps is not None:
        for comp in comps:
            if _lname(comp.tag) != 'component':
                continue
            cobjid = comp.get('objectid')
            cpath = _get_attr(comp, 'path') or file_path
            cA, cT = _parse_transform(
                comp.get('transform'), f"component objectid={cobjid}"
            )
            # submodel-local -> object coords via (cA, cT); then -> global via (A, t)
            newA = _matmul3(cA, A)
            newT = list(_apply(A, t, cT))
            _collect_global_corners(
                cpath, cobjid, newA, newT, get_root, corners, depth + 1
            )


def _collect_build_items(model_root):
    """
    Return the de-duplicated list of <item> elements under the build section,
    matching both namespaced and non-namespaced findall passes (some files
    omit the namespace). De-dup is by element identity so a single delta is
    applied exactly once per item.
    """
    seen = set()
    items = []
    for finder in (
        lambda: model_root.findall(f'.//{{{CORE_NS}}}item'),
        lambda: model_root.findall('.//item'),
    ):
        for item in finder():
            if id(item) not in seen:
                seen.add(id(item))
                items.append(item)
    return items


def _fmt_transform(A, t):
    return ' '.join(repr(v) for v in (list(A) + list(t)))


def recenter_and_drop_model(model_root, get_submodel_root, bed):
    """
    Rigidly translate the whole build as a group so its mesh-derived XY
    bounding box is centered on the bed, and drop each item to the bed by its
    own world-space minimum Z. Relative layout and every item's rotation/scale
    (A) and inter-item offsets are preserved.

    - model_root: parsed root of 3dmodel.model (mutated in place).
    - get_submodel_root: callable(path)->root or None for component submodels.
    - bed: BedBounds (target center + fit check).

    Raises ConversionError on: no build items, no resolvable geometry anywhere,
    malformed transforms, or a group that cannot fit the printable area.
    """
    def get_root(path):
        if path is _MAIN_MODEL:
            return model_root
        return get_submodel_root(path)

    items = _collect_build_items(model_root)
    if not items:
        raise ConversionError("3dmodel.model has no build <item>; nothing to place.")

    resolved = []  # (item_elem, A, t, world_minz_or_None)
    g_minx = g_miny = math.inf
    g_maxx = g_maxy = -math.inf
    any_geom = False

    for item in items:
        objid = item.get('objectid')
        A, t = _parse_transform(item.get('transform'), f"build item objectid={objid}")
        corners = []
        _collect_global_corners(_MAIN_MODEL, objid, A, t, get_root, corners)
        if corners:
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            zs = [c[2] for c in corners]
            imnx, imxx = min(xs), max(xs)
            imny, imxy = min(ys), max(ys)
            imnz = min(zs)
            g_minx, g_maxx = min(g_minx, imnx), max(g_maxx, imxx)
            g_miny, g_maxy = min(g_miny, imny), max(g_maxy, imxy)
            any_geom = True
            resolved.append((item, A, t, imnz))
        else:
            print(
                f"WARN: build item objectid={objid} has no resolvable geometry; "
                f"applying group shift but skipping drop-to-bed."
            )
            resolved.append((item, A, t, None))

    if not any_geom:
        raise ConversionError(
            "No build item has resolvable geometry; cannot recenter the model."
        )

    group_w = g_maxx - g_minx
    group_h = g_maxy - g_miny
    if group_w > bed.width + _Z_DROP_EPS or group_h > bed.height + _Z_DROP_EPS:
        raise ConversionError(
            f"Model spans {group_w:.1f}mm x {group_h:.1f}mm but the U1 printable "
            f"area is only {bed.width:.1f}mm x {bed.height:.1f}mm; it will not fit. "
            f"Re-export a single plate that fits the bed."
        )

    group_cx = (g_minx + g_maxx) / 2.0
    group_cy = (g_miny + g_maxy) / 2.0
    dx = bed.center_x - group_cx
    dy = bed.center_y - group_cy

    for item, A, t, world_minz in resolved:
        tx = t[0] + dx
        ty = t[1] + dy
        tz = t[2]
        if world_minz is not None and abs(world_minz) > _Z_DROP_EPS:
            tz = t[2] - world_minz
        item.set('transform', _fmt_transform(A, [tx, ty, tz]))


def count_plates(model_settings_root):
    """Number of <plate> blocks in a parsed model_settings.config root."""
    return len(model_settings_root.findall('.//plate'))


def convert_single_file(input_path, output_path, user_colors):
    """
    Convert a single Bambu .3mf file to Snapmaker U1 format.

    Args:
        input_path: Path to the input Bambu .3mf file
        output_path: Path where the converted file will be saved
        user_colors: Dict {original_filament_id: {color: #hex, type: PLA}}

    Returns:
        (success, error_message) - Tuple with success bool and error string if failed
    """
    src_name = os.path.basename(input_path)

    # 1. Read original file's project settings first to determine template
    try:
        with zipfile.ZipFile(input_path, 'r') as z_orig:
            original_project_settings = json.loads(z_orig.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return (False, f'Could not read original project settings: {e}')

    # 2. Multi-plate policy: the U1 prints a single plate. Refuse (loudly) any
    #    file that carries more than one plate rather than silently collapsing
    #    or mis-placing objects. Per-plate splitting is a planned follow-up.
    try:
        with zipfile.ZipFile(input_path, 'r') as z_orig:
            if 'Metadata/model_settings.config' in z_orig.namelist():
                ms_root = ET.fromstring(
                    z_orig.read('Metadata/model_settings.config').decode('utf-8')
                )
                plate_count = count_plates(ms_root)
                if plate_count > 1:
                    return (
                        False,
                        f"{src_name} contains {plate_count} plates; the U1 prints "
                        f"one plate — re-export a single plate",
                    )
    except ConversionError:
        raise
    except Exception as e:
        return (False, f'Could not read model settings: {e}')

    # Determine which template to use based on support settings
    different_settings = original_project_settings.get('different_settings_to_system', [])
    has_support = any('enable_support' in s for s in different_settings if s)
    if has_support:
        template_file = 'u1_template_supports.3mf'
    else:
        template_file = 'u1_template.3mf'

    # 3. Read U1 Template's project settings and derive bed bounds from it.
    try:
        with zipfile.ZipFile(template_file, 'r') as z_templ:
            u1_project_settings_json = json.loads(z_templ.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return (False, f'U1 Template ({template_file}) not found on server: {e}')

    try:
        bed = parse_printable_area(u1_project_settings_json)
    except ConversionError as e:
        return (False, str(e))

    # 4. Copy the original file, then process the 3MF archive.
    shutil.copy(input_path, output_path)
    temp_zip = output_path + ".temp"

    try:
        with zipfile.ZipFile(output_path, 'r') as zin:
            with zipfile.ZipFile(temp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                # Get the original slice_info.config to modify it. Many valid
                # 3MFs omit it entirely — synthesize a minimal config instead
                # of failing the whole conversion (filaments/colors live in
                # project_settings.config, which is handled separately).
                if 'Metadata/slice_info.config' in zin.namelist():
                    slice_info_content = zin.read('Metadata/slice_info.config')
                else:
                    slice_info_content = b'<?xml version="1.0" encoding="UTF-8"?>\n<config/>'

                # --- Start Slice Info Modification ---
                # Change machine model
                xml_str = slice_info_content.decode('utf-8')
                xml_str = re.sub(r'key="printer_model_id" value="[^"]*"', r'key="printer_model_id" value="Snapmaker U1"', xml_str)
                root = ET.fromstring(xml_str)

                # Find the parent of the filament nodes (usually a 'plate' or the root)
                filaments_parent = root.find('.//plate')
                if filaments_parent is None:
                    filaments_parent = root

                all_fil_nodes = filaments_parent.findall('.//filament')

                # Get original filaments in order to map them
                original_filaments = parse_bambu_filaments(input_path)

                # Keep track of which original filaments are being used
                used_original_ids = user_colors.keys()

                # Remove unused filament nodes from the XML
                for fil_node in all_fil_nodes:
                    if fil_node.get('id') not in used_original_ids:
                        filaments_parent.remove(fil_node)

                # Build the ID mapping: old_id -> new_id
                # This is CRITICAL for updating model_settings.config
                id_mapping = {}
                new_id_counter = 1  # U1/Orca uses 1-based IDs for extruders

                # Build the mapping from the filaments the user actually kept,
                # in original order. This MUST NOT depend on slice_info.config
                # carrying <filament> nodes: many Bambu exports leave slice_info
                # empty and describe filaments only in project_settings.config.
                # Tying the mapping to a slice_info node lookup previously left
                # id_mapping empty for those files, so painted-region extruder
                # references were never remapped. When the original ids were
                # already 1..N that was harmless by accident, but a deselected
                # or non-sequential filament silently pointed painted regions at
                # the wrong (padded white) extruder — losing the multi-color.
                for original_fil in original_filaments:
                    original_id = original_fil['id']
                    if original_id in user_colors:
                        new_conf = user_colors[original_id]

                        # Store the mapping (old Bambu id -> sequential U1 id).
                        id_mapping[original_id] = str(new_id_counter)

                        # Rewrite the slice_info <filament> node when present;
                        # its absence must not break the mapping above.
                        node_to_update = filaments_parent.find(f".//filament[@id='{original_id}']")
                        if node_to_update is not None:
                            node_to_update.set('id', str(new_id_counter))
                            node_to_update.set('color', new_conf['color'])
                            node_to_update.set('type', new_conf['type'])

                        new_id_counter += 1

                # Add dummy filaments to reach 4 (white PLA)
                TARGET_FILAMENTS = 4
                while new_id_counter <= TARGET_FILAMENTS:
                    dummy_fil = ET.SubElement(filaments_parent, 'filament')
                    dummy_fil.set('id', str(new_id_counter))
                    dummy_fil.set('type', 'PLA')
                    dummy_fil.set('color', '#FFFFFFFF')
                    dummy_fil.set('used_m', '0')
                    dummy_fil.set('used_g', '0')
                    new_id_counter += 1

                modified_slice_info = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                # --- End Slice Info Modification ---

                # --- Start Model Settings Modification ---
                # We need to update extruder references in model_settings.config
                model_settings_content = zin.read('Metadata/model_settings.config')
                model_root = ET.fromstring(model_settings_content.decode('utf-8'))

                # Find all extruder metadata tags and update them. Every
                # painted-region extruder reference MUST remap to a kept
                # filament; an unmapped reference would silently point at a
                # non-existent extruder and corrupt the multi-color output.
                for metadata in model_root.findall('.//metadata[@key="extruder"]'):
                    old_extruder = metadata.get('value')
                    if old_extruder in id_mapping:
                        metadata.set('value', id_mapping[old_extruder])
                    else:
                        raise ConversionError(
                            f"model_settings.config references extruder "
                            f"'{old_extruder}' which has no filament mapping "
                            f"(mapped ids: {sorted(id_mapping.keys())}). The "
                            f"selected filaments do not cover every painted "
                            f"region."
                        )

                # NOTE: drop-to-bed is handled per build item in
                # recenter_and_drop_model (world-min-z), NOT by blanket-zeroing
                # every part matrix — that destroyed intentional relative Z
                # between parts (e.g. a button sitting on top of a body).

                modified_model_settings = ET.tostring(model_root, encoding='utf-8', xml_declaration=True)
                # --- End Model Settings Modification ---

                # --- Start 3D Model Transform Modification (Auto-center) ---
                # Rigidly translate the whole build as a group onto the U1 bed
                # center and drop each item to the bed by its own world-min-Z.
                modified_3d_model = None
                if '3D/3dmodel.model' not in zin.namelist():
                    raise ConversionError(
                        f"{src_name} has no 3D/3dmodel.model; not a valid 3MF."
                    )

                # Register namespaces so they are preserved in output.
                namespaces = {
                    '': CORE_NS,
                    'p': PROD_NS,
                    'BambuStudio': 'http://schemas.bambulab.com/package/2021',
                }
                for prefix, uri in namespaces.items():
                    ET.register_namespace(prefix, uri)

                model_3d_root = ET.fromstring(
                    zin.read('3D/3dmodel.model').decode('utf-8')
                )

                # Cache parsed submodel roots read from the input archive.
                _submodel_cache = {}

                def _read_submodel(path):
                    name = str(path).lstrip('/')
                    if name in _submodel_cache:
                        return _submodel_cache[name]
                    root = None
                    if name in zin.namelist():
                        try:
                            root = ET.fromstring(zin.read(name).decode('utf-8'))
                        except ET.ParseError as e:
                            raise ConversionError(
                                f"Corrupt submodel '{name}': {e}"
                            )
                    _submodel_cache[name] = root
                    return root

                recenter_and_drop_model(model_3d_root, _read_submodel, bed)

                modified_3d_model = ET.tostring(
                    model_3d_root, encoding='utf-8', xml_declaration=True
                )
                # --- End 3D Model Transform Modification ---

                # --- Start Project Settings Modification ---
                # Combine U1 printer settings with user-selected filament colors
                # Start with U1 template settings (for printer configuration)
                combined_project_settings = u1_project_settings_json.copy()

                # Get the number of filaments from the original file
                original_filaments = parse_bambu_filaments(input_path)

                # Build the new filament colors list based on user selections
                # user_colors is keyed by original filament ID
                new_filament_colors = []
                new_filament_types = []

                # Iterate through original filaments in order
                for orig_fil in original_filaments:
                    orig_id = orig_fil['id']
                    if orig_id in user_colors:
                        # User provided color/type for this filament
                        color = user_colors[orig_id]['color']
                        fil_type = user_colors[orig_id]['type']
                    else:
                        # Keep original color/type
                        color = orig_fil['color']
                        fil_type = orig_fil['type']

                    # Ensure color format is correct (with alpha for compatibility)
                    if len(color) == 7:  # #RRGGBB
                        color = color + 'FF'  # Add alpha
                    new_filament_colors.append(color.upper())
                    new_filament_types.append(fil_type)

                # Ensure at least 4 filaments (U1 minimum), but allow more
                MIN_FILAMENTS = 4
                DEFAULT_COLOR = '#FFFFFFFF'
                DEFAULT_TYPE = 'PLA'
                num_filaments = max(len(new_filament_colors), MIN_FILAMENTS)

                while len(new_filament_colors) < MIN_FILAMENTS:
                    new_filament_colors.append(DEFAULT_COLOR)
                    new_filament_types.append(DEFAULT_TYPE)

                # Update filament colors in combined settings
                combined_project_settings['filament_colour'] = new_filament_colors

                # Update filament types
                combined_project_settings['filament_type'] = new_filament_types

                # Map filament types to Snapmaker U1 filament profiles
                # Using profiles loaded from filament_types.3mf
                filament_profile_map = {f['type']: f['settings_id'] for f in AVAILABLE_FILAMENTS}
                default_profile = AVAILABLE_FILAMENTS[0]['settings_id'] if AVAILABLE_FILAMENTS else 'Snapmaker PLA SnapSpeed @U1'

                new_filament_settings_ids = []
                for fil_type in new_filament_types:
                    profile = filament_profile_map.get(fil_type, default_profile)
                    new_filament_settings_ids.append(profile)
                combined_project_settings['filament_settings_id'] = new_filament_settings_ids

                # Adjust other filament arrays to match actual filament count
                for key in combined_project_settings:
                    if key.startswith('filament_') and isinstance(combined_project_settings[key], list):
                        current_len = len(combined_project_settings[key])
                        if current_len > 0 and current_len != num_filaments:
                            if num_filaments > current_len:
                                # Extend by repeating the last value
                                last_val = combined_project_settings[key][-1]
                                combined_project_settings[key].extend([last_val] * (num_filaments - current_len))
                            else:
                                # Truncate to match actual count
                                combined_project_settings[key] = combined_project_settings[key][:num_filaments]

                # Convert to JSON string
                combined_project_settings_str = json.dumps(combined_project_settings, indent=4, ensure_ascii=False)
                # --- End Project Settings Modification ---

                # Write the modified archive
                for item in zin.infolist():
                    # Replace project settings, slice info, model settings, and 3D model
                    if item.filename == 'Metadata/project_settings.config':
                        zout.writestr(item, combined_project_settings_str.encode('utf-8'))
                    elif item.filename == 'Metadata/slice_info.config':
                        zout.writestr(item, modified_slice_info)
                    elif item.filename == 'Metadata/model_settings.config':
                        zout.writestr(item, modified_model_settings)
                    elif item.filename == '3D/3dmodel.model' and modified_3d_model is not None:
                        zout.writestr(item, modified_3d_model)
                    else:
                        # Copy all other files as-is
                        content = zin.read(item.filename)
                        zout.writestr(item, content)

                # If the source had no slice_info.config, add the synthesized
                # (padded-filament) one so the output is a complete U1 project.
                if 'Metadata/slice_info.config' not in zin.namelist():
                    zout.writestr('Metadata/slice_info.config', modified_slice_info)

        shutil.move(temp_zip, output_path)
        return (True, None)

    except ConversionError as e:
        # Expected, actionable failure: surface the message, still log context.
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        if os.path.exists(output_path):
            os.remove(output_path)
        traceback.print_exc()
        return (False, str(e))
    except Exception as e:
        # Unexpected failure: log full traceback so it is never swallowed.
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        if os.path.exists(output_path):
            os.remove(output_path)
        traceback.print_exc()
        return (False, str(e))


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/filament-types')
def get_filament_types():
    """Return available filament types for the frontend dropdown."""
    return jsonify(AVAILABLE_FILAMENTS)

@app.route('/analyze', methods=['POST'])
def analyze():
    # Cleanup old files on each upload
    cleanup_old_files()

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Generate unique session ID
    session_id = str(uuid.uuid4())[:8]
    input_filename = f"{session_id}_input.3mf"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], input_filename)
    file.save(filepath)

    # Analyze colors
    filaments = parse_bambu_filaments(filepath)

    # Get original filename for later use
    original_name = os.path.splitext(file.filename)[0]

    return jsonify({
        'session_id': session_id,
        'filaments': filaments,
        'original_name': original_name
    })

@app.route('/convert', methods=['POST'])
def convert():
    data = request.json
    session_id = data.get('session_id')
    if not session_id:
        return jsonify({'error': 'No session ID provided'}), 400

    original_name = data.get('original_name', 'Converted')
    input_filename = f"{session_id}_input.3mf"
    output_filename = f"{session_id}_U1_Ready.3mf"
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], input_filename)
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)

    if not os.path.exists(input_path):
        return jsonify({'error': 'Session expired or file not found'}), 404

    user_colors = data.get('colors', {})  # Dict {original_filament_id: {color: #hex, type: PLA}}

    success, error = convert_single_file(input_path, output_path, user_colors)

    if success:
        # Return download URL with original name for proper download filename
        download_name = f"{original_name}_U1.3mf"
        return jsonify({
            'download_url': f'/download/{output_filename}',
            'download_name': download_name
        })
    else:
        return jsonify({'error': error}), 500


@app.route('/batch-analyze', methods=['POST'])
def batch_analyze():
    """Analyze multiple uploaded .3mf files for batch conversion."""
    cleanup_old_files()

    if 'files[]' not in request.files:
        return jsonify({'error': 'No files uploaded'}), 400

    files = request.files.getlist('files[]')
    if not files or len(files) == 0:
        return jsonify({'error': 'No files selected'}), 400

    # Generate batch session ID
    batch_session_id = str(uuid.uuid4())[:8]
    batch_folder = os.path.join(app.config['UPLOAD_FOLDER'], f"batch_{batch_session_id}")
    os.makedirs(batch_folder, exist_ok=True)

    bambu_files = []
    skipped_files = []

    for file in files:
        if not file.filename or not file.filename.lower().endswith('.3mf'):
            continue

        # Save file temporarily
        safe_filename = os.path.basename(file.filename)
        filepath = os.path.join(batch_folder, safe_filename)
        file.save(filepath)

        # Check if it's a Bambu file
        if is_bambu_file(filepath):
            filaments = parse_bambu_filaments(filepath)
            auto_colors = auto_map_filaments(filaments)
            bambu_files.append({
                'filename': safe_filename,
                'filaments': filaments,
                'auto_colors': auto_colors
            })
        else:
            skipped_files.append({
                'filename': safe_filename,
                'reason': 'Already Snapmaker or not a Bambu file'
            })
            os.remove(filepath)

    if not bambu_files:
        # Clean up empty batch folder
        shutil.rmtree(batch_folder, ignore_errors=True)
        return jsonify({'error': 'No valid Bambu Lab .3mf files found'}), 400

    return jsonify({
        'batch_session_id': batch_session_id,
        'files': bambu_files,
        'skipped': skipped_files
    })


@app.route('/batch-convert', methods=['POST'])
def batch_convert():
    """Convert all files in a batch session."""
    data = request.json
    batch_session_id = data.get('batch_session_id')
    if not batch_session_id:
        return jsonify({'error': 'No batch session ID provided'}), 400

    batch_folder = os.path.join(app.config['UPLOAD_FOLDER'], f"batch_{batch_session_id}")
    if not os.path.exists(batch_folder):
        return jsonify({'error': 'Batch session expired or not found'}), 404

    # Get output folder from settings
    settings = history_manager.get_settings()
    output_folder = settings.get('output_folder', './converted_u1')
    os.makedirs(output_folder, exist_ok=True)

    files_to_convert = data.get('files', [])
    if not files_to_convert:
        files_to_convert = [f for f in os.listdir(batch_folder) if f.endswith('.3mf')]

    converted_files = []
    skipped_files = []
    errors = []

    for file_info in files_to_convert:
        if isinstance(file_info, dict):
            filename = file_info.get('filename')
            auto_colors = file_info.get('auto_colors', {})
        else:
            filename = file_info
            input_path = os.path.join(batch_folder, filename)
            filaments = parse_bambu_filaments(input_path)
            auto_colors = auto_map_filaments(filaments)

        input_path = os.path.join(batch_folder, filename)
        if not os.path.exists(input_path):
            errors.append({'filename': filename, 'error': 'File not found'})
            continue

        # Calculate hash for duplicate detection
        file_hash = history_manager.hash_file(input_path)

        # Check if exact duplicate
        existing = history_manager.find_by_hash(file_hash)
        if existing and settings.get('delete_duplicates', True):
            skipped_files.append({'filename': filename, 'reason': 'Already converted'})
            continue

        # Generate output filename with versioning
        base_name = os.path.splitext(filename)[0]
        output_filename = f"{base_name}_U1.3mf"
        output_path = os.path.join(output_folder, output_filename)

        # Version if exists
        version = 2
        while os.path.exists(output_path):
            output_filename = f"{base_name}_U1_v{version}.3mf"
            output_path = os.path.join(output_folder, output_filename)
            version += 1

        filaments = parse_bambu_filaments(input_path)
        success, error = convert_single_file(input_path, output_path, auto_colors)

        if success:
            converted_files.append(output_filename)
            history_manager.add_converted(filename, file_hash, output_filename, len(filaments))
        else:
            errors.append({'filename': filename, 'error': error})

    # Clean up batch folder
    shutil.rmtree(batch_folder, ignore_errors=True)

    return jsonify({
        'converted_count': len(converted_files),
        'converted_files': converted_files,
        'skipped_count': len(skipped_files),
        'skipped_files': skipped_files,
        'error_count': len(errors),
        'errors': errors,
        'output_folder': output_folder
    })


@app.route('/download/<path:filename>')
def download_file(filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    # Use provided download name or determine based on file type
    download_name = request.args.get('name')
    if not download_name:
        if filename.endswith('.zip'):
            download_name = 'Snapmaker_U1_Batch.zip'
        else:
            download_name = 'Snapmaker_U1_Ready.3mf'

    return send_file(filepath, as_attachment=True, download_name=download_name)


@app.route('/settings', methods=['GET'])
def get_settings():
    """Get current settings."""
    return jsonify(history_manager.get_settings())


@app.route('/settings', methods=['POST'])
def update_settings():
    """Update settings."""
    data = request.json
    history_manager.update_settings(data)
    return jsonify({'success': True, 'settings': history_manager.get_settings()})


@app.route('/history', methods=['GET'])
def get_history():
    """Get conversion history."""
    return jsonify(history_manager.get_converted())


@app.route('/history/clear', methods=['POST'])
def clear_history():
    """Clear conversion history."""
    history_manager.clear_history()
    return jsonify({'success': True})


@app.route('/browse', methods=['GET'])
def browse_directory():
    """List contents of a directory for folder browser."""
    path = request.args.get('path', '')

    # Default to common roots
    if not path:
        # Return drive letters on Windows, root dirs on Linux
        if os.name == 'nt':
            import string
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return jsonify({'path': '', 'dirs': drives, 'is_root': True})
        else:
            path = '/'

    # Normalize path
    path = os.path.normpath(path)

    if not os.path.isdir(path):
        return jsonify({'error': 'Path is not a directory'}), 400

    try:
        entries = []
        for name in sorted(os.listdir(path)):
            full_path = os.path.join(path, name)
            if os.path.isdir(full_path):
                entries.append({
                    'name': name,
                    'path': full_path,
                    'type': 'dir'
                })
        return jsonify({
            'path': path,
            'parent': os.path.dirname(path) if path != '/' else None,
            'dirs': entries
        })
    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403


@app.route('/check-new', methods=['GET'])
def check_new_files():
    """Find unconverted files in source folder."""
    settings = history_manager.get_settings()
    source_folder = settings.get('source_folder', '')

    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({'new_files': [], 'count': 0, 'error': 'Source folder not configured'})

    new_files = []

    for filename in os.listdir(source_folder):
        if not filename.lower().endswith('.3mf'):
            continue

        filepath = os.path.join(source_folder, filename)
        if not os.path.isfile(filepath):
            continue

        # Check if it's a Bambu file
        if not is_bambu_file(filepath):
            continue

        # Hash the file
        file_hash = history_manager.hash_file(filepath)

        # Check if already converted
        existing = history_manager.find_by_hash(file_hash)
        if existing:
            continue

        # Parse filaments for preview
        filaments = parse_bambu_filaments(filepath)

        new_files.append({
            'filename': filename,
            'filepath': filepath,
            'hash': file_hash,
            'filaments': len(filaments)
        })

    return jsonify({'new_files': new_files, 'count': len(new_files)})


@app.route('/convert-new', methods=['POST'])
def convert_new_files():
    """Convert all new files from the source folder to the output folder."""
    settings = history_manager.get_settings()
    source_folder = settings.get('source_folder', '')
    output_folder = settings.get('output_folder', './converted_u1')

    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({'error': 'Source folder not configured'}), 400

    os.makedirs(output_folder, exist_ok=True)

    converted_files = []
    skipped_files = []
    errors = []

    for filename in os.listdir(source_folder):
        if not filename.lower().endswith('.3mf'):
            continue

        filepath = os.path.join(source_folder, filename)
        if not os.path.isfile(filepath):
            continue

        # Check if it's a Bambu file
        if not is_bambu_file(filepath):
            continue

        # Hash the file
        file_hash = history_manager.hash_file(filepath)

        # Check if already converted
        existing = history_manager.find_by_hash(file_hash)
        if existing and settings.get('delete_duplicates', True):
            skipped_files.append({'filename': filename, 'reason': 'Already converted'})
            continue

        # Parse filaments and auto-map
        filaments = parse_bambu_filaments(filepath)
        auto_colors = auto_map_filaments(filaments)

        # Generate output filename with versioning
        base_name = os.path.splitext(filename)[0]
        output_filename = f"{base_name}_U1.3mf"
        output_path = os.path.join(output_folder, output_filename)

        # Version if exists
        version = 2
        while os.path.exists(output_path):
            output_filename = f"{base_name}_U1_v{version}.3mf"
            output_path = os.path.join(output_folder, output_filename)
            version += 1

        success, error = convert_single_file(filepath, output_path, auto_colors)

        if success:
            converted_files.append(output_filename)
            history_manager.add_converted(filename, file_hash, output_filename, len(filaments))
        else:
            errors.append({'filename': filename, 'error': error})

    return jsonify({
        'converted_count': len(converted_files),
        'converted_files': converted_files,
        'skipped_count': len(skipped_files),
        'skipped_files': skipped_files,
        'error_count': len(errors),
        'errors': errors,
        'output_folder': output_folder
    })


@app.route('/convert-file', methods=['POST'])
def convert_single_source_file():
    """Convert a single file from the source folder."""
    data = request.json
    filepath = data.get('filepath')

    if not filepath:
        return jsonify({'error': 'No filepath provided'}), 400

    if not os.path.isfile(filepath):
        return jsonify({'error': 'File not found'}), 404

    settings = history_manager.get_settings()
    output_folder = settings.get('output_folder', './converted_u1')
    os.makedirs(output_folder, exist_ok=True)

    filename = os.path.basename(filepath)

    # Check if it's a Bambu file
    if not is_bambu_file(filepath):
        return jsonify({'error': 'Not a Bambu Lab file', 'skipped': True}), 200

    # Hash the file
    file_hash = history_manager.hash_file(filepath)

    # Check if already converted
    existing = history_manager.find_by_hash(file_hash)
    if existing and settings.get('delete_duplicates', True):
        return jsonify({'skipped': True, 'reason': 'Already converted'}), 200

    # Parse filaments and auto-map
    filaments = parse_bambu_filaments(filepath)
    auto_colors = auto_map_filaments(filaments)

    # Generate output filename with versioning
    base_name = os.path.splitext(filename)[0]
    output_filename = f"{base_name}_U1.3mf"
    output_path = os.path.join(output_folder, output_filename)

    # Version if exists
    version = 2
    while os.path.exists(output_path):
        output_filename = f"{base_name}_U1_v{version}.3mf"
        output_path = os.path.join(output_folder, output_filename)
        version += 1

    success, error = convert_single_file(filepath, output_path, auto_colors)

    if success:
        history_manager.add_converted(filename, file_hash, output_filename, len(filaments))
        return jsonify({
            'success': True,
            'output_filename': output_filename,
            'filaments': len(filaments)
        })
    else:
        return jsonify({'error': error}), 500


if __name__ == '__main__':
    import os
    debug = os.environ.get('FLASK_ENV', 'development') == 'development'
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=debug, host='0.0.0.0', port=port)