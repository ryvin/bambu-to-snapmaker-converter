import os
import zipfile
import shutil
import re
import json
import copy
import uuid
import time
import math
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from flask import Flask, render_template, request, send_file, jsonify
from history import HistoryManager

# Per-filament project_settings.config keys that do NOT start with 'filament_'
# and therefore were previously left at the template's length 4, producing an
# inconsistent output (filament arrays length N, these length 4) that Snapmaker
# Orca rejects for N>4-color files. These MUST be resized to the filament count.
# Ground truth: keys that track filament count in Snapmaker-Orca's own re-saved
# U1 files at N=5/6/7 (e.g. 580MM_U1_v3.3mf, ButterflyWing_fans_U1.3mf), 2026-08.
# Per-EXTRUDER keys (nozzle_diameter, extruder_*, retraction_*, wipe*, z_hop*)
# are intentionally EXCLUDED — the U1 is 4 single-nozzle tools, so they stay 4.
U1_PER_FILAMENT_EXTRA_KEYS = frozenset({
    'activate_air_filtration', 'activate_chamber_temp_control',
    'adaptive_pressure_advance', 'adaptive_pressure_advance_bridges',
    'adaptive_pressure_advance_model', 'adaptive_pressure_advance_overhangs',
    'additional_cooling_fan_speed', 'chamber_temperature',
    'close_fan_the_first_x_layers', 'complete_print_exhaust_fan_speed',
    'cool_plate_temp', 'cool_plate_temp_initial_layer', 'default_filament_colour',
    'dont_slow_down_outer_wall', 'during_print_exhaust_fan_speed',
    'enable_overhang_bridge_fan', 'enable_pressure_advance',
    'eng_plate_temp', 'eng_plate_temp_initial_layer', 'fan_cooling_layer_time',
    'fan_max_speed', 'fan_min_speed', 'full_fan_speed_layer',
    'graphic_effect_plate_temp', 'graphic_effect_plate_temp_initial_layer',
    'hot_plate_temp', 'hot_plate_temp_initial_layer', 'idle_temperature',
    'internal_bridge_fan_speed', 'ironing_fan_speed',
    'nozzle_temperature', 'nozzle_temperature_initial_layer',
    'nozzle_temperature_range_high', 'nozzle_temperature_range_low',
    'overhang_fan_speed', 'overhang_fan_threshold', 'pellet_flow_coefficient',
    'pressure_advance', 'reduce_fan_stop_start_freq', 'required_nozzle_HRC',
    'slow_down_for_layer_cooling', 'slow_down_layer_time', 'slow_down_min_speed',
    'supertack_plate_temp', 'supertack_plate_temp_initial_layer',
    'support_material_interface_fan_speed', 'temperature_vitrification',
    'textured_cool_plate_temp', 'textured_cool_plate_temp_initial_layer',
    'textured_plate_temp', 'textured_plate_temp_initial_layer',
})

# Value Snapmaker Orca writes into new inter-filament flush cells when it grows
# a 4-filament project to N (any positive value loads; Orca recomputes from
# colors on demand). Verified against Orca's own 4->7 re-save.
_FLUSH_FILL = '280'


def _resize_filament_settings(ps, num_filaments):
    """Resize every PER-FILAMENT array in a project_settings dict to
    ``num_filaments`` (in place), and rebuild the inter-filament flush matrix
    (N*N, diagonal '0') and vector (2N).

    Per-filament = keys starting with ``filament_`` plus the non-prefixed
    per-filament keys in ``U1_PER_FILAMENT_EXTRA_KEYS`` (temperatures, plate
    temps, fan speeds, pressure advance, ...). PER-EXTRUDER keys (nozzle_diameter,
    extruder_*, retraction_*, wipe*, z_hop*) and per-plate keys (wipe_tower_x/y)
    are intentionally left untouched — the U1 is 4 single-nozzle tools.
    Empty lists are left as-is. Raises ConversionError on a non-square flush
    matrix. Returns ``ps``.
    """
    for key, val in list(ps.items()):
        if not isinstance(val, list) or not val:
            continue
        if not (key.startswith('filament_') or key in U1_PER_FILAMENT_EXTRA_KEYS):
            continue
        cur = len(val)
        if cur == num_filaments:
            continue
        if num_filaments > cur:
            ps[key] = val + [val[-1]] * (num_filaments - cur)  # extend with last
        else:
            ps[key] = val[:num_filaments]

    matrix = ps.get('flush_volumes_matrix')
    if isinstance(matrix, list) and matrix:
        old_n = int(round(len(matrix) ** 0.5))
        if old_n * old_n != len(matrix):
            raise ConversionError(
                f"flush_volumes_matrix is not square (len={len(matrix)}); "
                f"cannot rebuild for {num_filaments} filaments."
            )
        ps['flush_volumes_matrix'] = [
            '0' if i == j
            else matrix[i * old_n + j] if (i < old_n and j < old_n)
            else _FLUSH_FILL
            for i in range(num_filaments) for j in range(num_filaments)
        ]

    vector = ps.get('flush_volumes_vector')
    if isinstance(vector, list) and vector:
        target = 2 * num_filaments
        ps['flush_volumes_vector'] = (
            vector + [vector[-1]] * (target - len(vector)) if len(vector) < target
            else vector[:target])
    return ps


def _resize_plate_settings(ps, n_plates):
    """Per-plate project_settings arrays must have one entry per plate. Only
    ``wipe_tower_x``/``wipe_tower_y`` are per-plate for the U1 (verified against
    Orca's own multi-plate files); repeat the template's bed-local value.
    ``first_layer_print_sequence`` etc. are NOT per-plate — left untouched."""
    for key in ('wipe_tower_x', 'wipe_tower_y'):
        val = ps.get(key)
        if isinstance(val, list) and val and len(val) != n_plates:
            ps[key] = ([val[0]] * n_plates)
    return ps


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


class _GeomIndex:
    """Per-model geometry index: everything recenter needs from a model file.

    objects: {object_id: {'aabb': (minx,miny,minz,maxx,maxy,maxz) or None,
                          'components': [(cobjid, transform_str, path_raw), ...]}}
    transform_str/path_raw are RAW attribute values (possibly None); parsing and
    the path fallback happen at consumption time in _collect_global_corners so
    error timing/messages stay byte-identical to the DOM version.
    """
    __slots__ = ('objects',)

    def __init__(self, objects):
        self.objects = objects


def _index_from_dom(root):
    """Build a _GeomIndex from an already-parsed model/submodel Element.

    Lifts, verbatim, the per-object reads _collect_global_corners historically
    did on the DOM (first <components> child only; first <mesh>/<vertices> via
    _object_local_aabb) so the DOM path stays equal by construction.
    """
    objects = {}
    for oid, obj in _object_map(root).items():
        comps_list = []
        comps = _find_child(obj, 'components')
        if comps is not None:
            for comp in comps:
                if _lname(comp.tag) != 'component':
                    continue
                comps_list.append((
                    comp.get('objectid'),
                    comp.get('transform'),
                    _get_attr(comp, 'path'),
                ))
        objects[oid] = {'aabb': _object_local_aabb(obj), 'components': comps_list}
    return _GeomIndex(objects)


def _as_geom_index(node):
    """Normalize get_root's return to a _GeomIndex: None->None, index->itself,
    parsed Element->_index_from_dom (cached on the element, best-effort)."""
    if node is None:
        return None
    if isinstance(node, _GeomIndex):
        return node
    cache = getattr(node, '_u1_geom_index', None)
    if cache is not None:
        return cache
    idx = _index_from_dom(node)
    try:
        node._u1_geom_index = idx
    except AttributeError:
        pass
    return idx


# Leaf elements (<vertex>/<triangle>) between container prunes while streaming;
# bounds transient element shells to a few MB regardless of input size.
_PRUNE_EVERY = 20000


class _ObjState:
    __slots__ = ('elem', 'oid', 'mesh_elem', 'verts_elem', 'comps_elem',
                 'components', 'found',
                 'minx', 'miny', 'minz', 'maxx', 'maxy', 'maxz', 'aabb')


def _stream_geom_index(fileobj):
    """One streaming pass over a model/submodel XML: per-object local AABB plus
    component list, with bounded memory. Namespace-agnostic (matches by local
    name). Raises ET.ParseError on malformed XML (the caller wraps it).

    Everything is read at 'start' events (attributes are complete there and we
    never read children), so nothing we clear on 'end' is ever read afterward.
    """
    entries = []          # _ObjState in document (start) order; folded last-wins
    stack = []            # open Element refs (pushed on start, popped on end)
    obj_stack = []        # open _ObjState refs (innermost last)
    leaf_count = 0

    for event, elem in ET.iterparse(fileobj, events=('start', 'end')):
        lname = _lname(elem.tag)

        if event == 'start':
            stack.append(elem)
            if lname == 'object':
                st = _ObjState()
                st.elem = elem
                st.oid = elem.get('id')
                st.mesh_elem = st.verts_elem = st.comps_elem = None
                st.components = []
                st.found = False
                st.minx = st.miny = st.minz = math.inf
                st.maxx = st.maxy = st.maxz = -math.inf
                st.aabb = None
                obj_stack.append(st)
                entries.append(st)
            elif obj_stack:
                st = obj_stack[-1]
                parent = stack[-2] if len(stack) >= 2 else None
                if lname == 'mesh':
                    if parent is st.elem and st.mesh_elem is None:
                        st.mesh_elem = elem
                elif lname == 'vertices':
                    if (st.mesh_elem is not None and parent is st.mesh_elem
                            and st.verts_elem is None):
                        st.verts_elem = elem
                elif lname == 'vertex':
                    if st.verts_elem is not None and parent is st.verts_elem:
                        try:
                            x = float(elem.get('x'))
                            y = float(elem.get('y'))
                            z = float(elem.get('z'))
                        except (TypeError, ValueError):
                            pass
                        else:
                            st.found = True
                            if x < st.minx:
                                st.minx = x
                            if x > st.maxx:
                                st.maxx = x
                            if y < st.miny:
                                st.miny = y
                            if y > st.maxy:
                                st.maxy = y
                            if z < st.minz:
                                st.minz = z
                            if z > st.maxz:
                                st.maxz = z
                elif lname == 'components':
                    if parent is st.elem and st.comps_elem is None:
                        st.comps_elem = elem
                elif lname == 'component':
                    if st.comps_elem is not None and parent is st.comps_elem:
                        st.components.append((
                            elem.get('objectid'),
                            elem.get('transform'),
                            _get_attr(elem, 'path'),
                        ))
            continue

        # event == 'end'
        stack.pop()
        if lname == 'vertex' or lname == 'triangle':
            leaf_count += 1
            if leaf_count % _PRUNE_EVERY == 0 and stack:
                del stack[-1][:]
        elif lname in ('vertices', 'triangles', 'mesh', 'components'):
            elem.clear()
        elif lname == 'object':
            st = obj_stack.pop()
            st.aabb = ((st.minx, st.miny, st.minz, st.maxx, st.maxy, st.maxz)
                       if st.found else None)
            elem.clear()

    objects = {}
    for st in entries:
        if st.oid is not None:
            objects[st.oid] = {'aabb': st.aabb, 'components': st.components}
    return _GeomIndex(objects)


def make_zip_submodel_reader(zin):
    """get_submodel_root factory for recenter_and_drop_model: streams each
    submodel from the open ZipFile ONCE into a _GeomIndex (never a full DOM),
    cached by normalized name. Missing entry -> None; malformed XML ->
    ConversionError("Corrupt submodel '<name>': ...")."""
    cache = {}
    names = set(zin.namelist())

    def _read(path):
        name = str(path).lstrip('/')
        if name in cache:
            return cache[name]
        idx = None
        if name in names:
            try:
                with zin.open(name) as f:
                    idx = _stream_geom_index(f)
            except ET.ParseError as e:
                raise ConversionError(f"Corrupt submodel '{name}': {e}")
        cache[name] = idx
        return idx

    return _read


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
    idx = _as_geom_index(get_root(file_path))
    if idx is None:
        return  # missing submodel file -> contributes no geometry
    info = idx.objects.get(str(object_id))
    if info is None:
        return

    aabb = info['aabb']
    if aabb is not None:
        minx, miny, minz, maxx, maxy, maxz = aabb
        for cx in (minx, maxx):
            for cy in (miny, maxy):
                for cz in (minz, maxz):
                    corners.append(_apply(A, t, (cx, cy, cz)))

    for cobjid, ctransform, cpath_raw in info['components']:
        cpath = cpath_raw or file_path
        cA, cT = _parse_transform(ctransform, f"component objectid={cobjid}")
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


def recenter_and_drop_model(model_root, get_submodel_root, bed, plate_groups=None):
    """
    Rigidly translate build items as group(s) so each group's mesh-derived XY
    bounding box is centered on a target, and drop each item to the bed by its
    own world-space minimum Z. Relative layout and every item's rotation/scale
    (A) and inter-item offsets are preserved.

    - model_root: parsed root of 3dmodel.model (mutated in place).
    - get_submodel_root: callable(path)->root or None for component submodels.
    - bed: BedBounds (fit check + single-plate target center).
    - plate_groups: None -> recenter ALL items as one group onto the bed center
      (single-plate; byte-identical to the pre-multiplate behaviour). Otherwise
      an ordered list of ``(object_id_set, (target_cx, target_cy))`` — each
      plate's items are recentered independently onto their target (grid cell)
      center, so all plates coexist in one file (Orca's plate grid).

    Raises ConversionError on: no build items, no resolvable geometry in a group,
    malformed transforms, a group that cannot fit the printable area, or an item
    that belongs to no plate / more than one plate.
    """
    _idx_memo = {}  # path (str or _MAIN_MODEL sentinel) -> _GeomIndex or None

    def get_root(path):
        # Returns a geometry index (or None). get_submodel_root may hand back a
        # parsed root, a _GeomIndex, or None; _as_geom_index normalizes either
        # way, and the memo derives the main-model index exactly once per convert.
        if path in _idx_memo:
            return _idx_memo[path]
        node = model_root if path is _MAIN_MODEL else get_submodel_root(path)
        idx = _as_geom_index(node)
        _idx_memo[path] = idx
        return idx

    items = _collect_build_items(model_root)
    if not items:
        raise ConversionError("3dmodel.model has no build <item>; nothing to place.")

    # Resolve each item once: transform, world-min-Z, and world XY bbox.
    resolved = []  # (item, A, t, world_minz_or_None, (minx,maxx,miny,maxy) or None)
    for item in items:
        objid = item.get('objectid')
        A, t = _parse_transform(item.get('transform'), f"build item objectid={objid}")
        corners = []
        _collect_global_corners(_MAIN_MODEL, objid, A, t, get_root, corners)
        if corners:
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            zs = [c[2] for c in corners]
            resolved.append((item, A, t, min(zs), (min(xs), max(xs), min(ys), max(ys))))
        else:
            print(
                f"WARN: build item objectid={objid} has no resolvable geometry; "
                f"applying group shift but skipping drop-to-bed."
            )
            resolved.append((item, A, t, None, None))

    # Build the groups to recenter. None -> one group of all items onto the bed
    # center (single-plate). Else one group per plate onto its grid-cell center.
    if plate_groups is None:
        groups = [(list(range(len(resolved))), (bed.center_x, bed.center_y), None)]
    else:
        obj_to_group = {}
        for gi, (idset, _center) in enumerate(plate_groups):
            for oid in idset:
                if oid in obj_to_group:
                    raise ConversionError(
                        f"object_id {oid} is assigned to more than one plate; cannot place it.")
                obj_to_group[oid] = gi
        members = [[] for _ in plate_groups]
        for ri, entry in enumerate(resolved):
            oid = entry[0].get('objectid')
            gi = obj_to_group.get(oid)
            if gi is None:
                raise ConversionError(
                    f"build item objectid={oid} is not on any plate; cannot place it.")
            members[gi].append(ri)
        groups = [(members[gi], plate_groups[gi][1], gi + 1) for gi in range(len(plate_groups))]

    for member_indices, (target_cx, target_cy), plate_label in groups:
        g_minx = g_miny = math.inf
        g_maxx = g_maxy = -math.inf
        any_geom = False
        for ri in member_indices:
            bbox = resolved[ri][4]
            if bbox is None:
                continue
            imnx, imxx, imny, imxy = bbox
            g_minx, g_maxx = min(g_minx, imnx), max(g_maxx, imxx)
            g_miny, g_maxy = min(g_miny, imny), max(g_maxy, imxy)
            any_geom = True
        if not any_geom:
            raise ConversionError(
                "No build item has resolvable geometry; cannot recenter the model."
                if plate_label is None
                else f"No build item on plate {plate_label} has resolvable geometry; cannot recenter it.")
        group_w = g_maxx - g_minx
        group_h = g_maxy - g_miny
        if group_w > bed.width + _Z_DROP_EPS or group_h > bed.height + _Z_DROP_EPS:
            noun = "Model" if plate_label is None else f"Plate {plate_label}"
            raise ConversionError(
                f"{noun} spans {group_w:.1f}mm x {group_h:.1f}mm but the U1 printable "
                f"area is only {bed.width:.1f}mm x {bed.height:.1f}mm; it will not fit. "
                f"Re-export a single plate that fits the bed."
            )
        dx = target_cx - (g_minx + g_maxx) / 2.0
        dy = target_cy - (g_miny + g_maxy) / 2.0
        for ri in member_indices:
            item, A, t, world_minz, _bbox = resolved[ri]
            tx = t[0] + dx
            ty = t[1] + dy
            tz = t[2]
            if world_minz is not None and abs(world_minz) > _Z_DROP_EPS:
                tz = t[2] - world_minz
            item.set('transform', _fmt_transform(A, [tx, ty, tz]))


def count_plates(model_settings_root):
    """Number of <plate> blocks in a parsed model_settings.config root."""
    return len(model_settings_root.findall('.//plate'))


# --- Multi-plate-in-one-file layout ------------------------------------------
# Snapmaker Orca (OrcaSlicer lineage) arranges plates in a grid whose stride is
# the printable-area size * (1 + 1/5); the per-plate offset is IMPLICIT from the
# plate's document order (NOT stored in the file), so a converted multi-plate
# file must place each plate's objects at exactly the cell Orca computes or the
# print lands off-bed. Constants verified against OrcaSlicer PartPlate.cpp and
# Snapmaker-Orca's own U1 files at N=2/5/6/8/10 (stride 324.0mm for the 270mm
# U1 bed, exact to the decimal). See reference_printer memory.
PLATE_GRID_GAP = 0.2  # LOGICAL_PART_PLATE_GAP = 1/5 (OrcaSlicer PartPlate.cpp)


def compute_plate_cols(n):
    """Grid column count Orca uses for n plates (PartPlate.hpp compute_colum_count):
    round(sqrt(n)), rounded up when sqrt is above the rounded value.
    1->1, 2..4->2, 5..9->3, 10..16->4."""
    v = math.sqrt(n)
    r = round(v)
    return int(r + 1) if v > r else int(r)


def plate_cell_center(k, cols, bed):
    """Bed-center of plate k (1-based document order) in the plate grid."""
    col, row = (k - 1) % cols, (k - 1) // cols
    stride = 1.0 + PLATE_GRID_GAP
    return (bed.center_x + col * bed.width * stride,
            bed.center_y - row * bed.height * stride)


def _plate_object_ids(plate_elem):
    """object_id values listed by a <plate> block's <model_instance> children."""
    ids = []
    for mi in plate_elem.findall('model_instance'):
        for md in mi.findall('metadata'):
            if md.get('key') == 'object_id':
                ids.append(md.get('value'))
    return ids


def convert_single_file(input_path, output_path, user_colors, merge_plates=True):
    """
    Convert a Bambu .3mf file to Snapmaker U1 format.

    Args:
        input_path: Path to the input Bambu .3mf file
        output_path: Path where the converted file will be saved
        user_colors: Dict {original_filament_id: {color: #hex, type: PLA}}
        merge_plates: When True (default) a multi-plate file is KEPT as one
            multi-plate U1 file, each plate recentered onto its Orca grid cell.
            When False a multi-plate file is refused (used by the legacy
            split-then-convert-each path, which feeds one plate at a time).

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

    # 2. Multi-plate handling. The U1 prints one plate at a time, but Snapmaker
    #    Orca keeps all plates in ONE file (a grid), so with merge_plates we KEEP
    #    every plate and recenter each onto its grid cell (built below, once the
    #    bed and build items are known). The legacy split path passes
    #    merge_plates=False and still refuses multi-plate (it feeds one plate at
    #    a time). n_plates<=1 -> single-plate, unchanged.
    n_plates = 1
    try:
        with zipfile.ZipFile(input_path, 'r') as z_orig:
            if 'Metadata/model_settings.config' in z_orig.namelist():
                ms_root = ET.fromstring(
                    z_orig.read('Metadata/model_settings.config').decode('utf-8')
                )
                n_plates = count_plates(ms_root)
                if n_plates > 1 and not merge_plates:
                    return (
                        False,
                        f"{src_name} contains {n_plates} plates; the U1 prints "
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

                # Stream each submodel from the archive once into a geometry
                # index (no full DOM of the 100+ MB meshes; they are only read
                # for their bounding boxes and copied byte-for-byte on output).
                _read_submodel = make_zip_submodel_reader(zin)

                # For a multi-plate file kept as one file, recenter each plate's
                # items onto its Orca grid cell. Validate membership up front
                # (like split_plates) so a bad file fails before any write.
                plate_groups = None
                if n_plates > 1 and merge_plates:
                    cols = compute_plate_cols(n_plates)
                    build_ids = {it.get('objectid')
                                 for it in _collect_build_items(model_3d_root)}
                    plate_groups = []
                    for k, plate in enumerate(ms_root.findall('.//plate'), start=1):
                        ids = _plate_object_ids(plate)
                        if not ids:
                            raise ConversionError(
                                f"plate {k} lists no model_instance objects; nothing to place.")
                        missing = sorted(i for i in ids if i not in build_ids)
                        if missing:
                            raise ConversionError(
                                f"plate {k} references object_id(s) {missing} with no "
                                f"matching build <item>; the file is inconsistent.")
                        plate_groups.append((set(ids), plate_cell_center(k, cols, bed)))

                recenter_and_drop_model(model_3d_root, _read_submodel, bed, plate_groups)

                modified_3d_model = ET.tostring(
                    model_3d_root, encoding='utf-8', xml_declaration=True
                )
                # --- End 3D Model Transform Modification ---

                # --- Start Project Settings Modification ---
                # Combine U1 printer settings with user-selected filament colors
                # Start with U1 template settings (for printer configuration)
                combined_project_settings = copy.deepcopy(u1_project_settings_json)

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

                # Make EVERY per-filament array (and the flush structures)
                # length N, so the output is internally consistent for any color
                # count the U1 supports — the fix for >4-color files that
                # Snapmaker Orca previously rejected.
                _resize_filament_settings(combined_project_settings, num_filaments)

                # Per-plate arrays (wipe_tower_x/y) must have one entry per plate.
                _resize_plate_settings(combined_project_settings, n_plates)

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
                    elif re.match(r'Metadata/plate_\d+\.json$', item.filename):
                        # Per-plate slice caches hold stale object bboxes after we
                        # recenter; they are optional, so drop them (Orca rebuilds).
                        continue
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


def _plate_num(path):
    m = re.search(r'_plate(\d+)', os.path.basename(path))
    return m.group(1) if m else '1'


def _clear_stale_outputs(save_dir, base_name):
    """Remove this base name's prior U1 outputs from ``save_dir`` before writing
    new ones, so a re-conversion never leaves stale files behind — e.g. old
    ``_plate5..8`` when the new run only produces 4 plates, or a leftover from an
    earlier buggy run. Matches exactly ``<base>_U1.3mf``, ``<base>_U1.zip`` and
    ``<base>_plate<N>_U1.3mf`` (N = digits); never touches other files.
    Returns the list of removed filenames.
    """
    if not save_dir or not os.path.isdir(save_dir):
        return []
    exact = {f"{base_name}_U1.3mf", f"{base_name}_U1.zip"}
    plate_rx = re.compile(r'^' + re.escape(base_name) + r'_plate\d+_U1\.3mf$')
    removed = []
    try:
        entries = os.listdir(save_dir)
    except OSError:
        return []
    for name in entries:
        if name in exact or plate_rx.match(name):
            try:
                os.remove(os.path.join(save_dir, name))
                removed.append(name)
            except OSError as e:
                print(f"WARN: could not remove stale output '{name}': {e}")
    if removed:
        print(f"Cleared {len(removed)} stale output(s) for '{base_name}' in {save_dir}")
    return removed


def convert_or_split_plates(input_path, output_path, user_colors, base_name='model', save_dir=None):
    """Convert a .3mf to Snapmaker U1, transparently handling multi-plate files.

    - Single plate: converts to output_path (.3mf); produced_path is that file.
    - Multiple plates: a 3MF can legitimately hold several plates, and the U1
      prints one plate at a time — so instead of refusing, split into one file
      per plate, convert each, and bundle the successes into a ZIP at
      ``<output_path stem>.zip`` (produced_path is the ZIP).

    If ``save_dir`` is set, the print-ready .3mf file(s) are also copied there
    with friendly names (e.g. ``<name>_U1.3mf`` / ``<name>_plateN_U1.3mf``).
    Returns ``(success, produced_path, message)``; message carries a warning
    (e.g. partial plate success) or the error on failure.
    """
    plate_count = 1
    try:
        with zipfile.ZipFile(input_path) as z:
            if 'Metadata/model_settings.config' in z.namelist():
                plate_count = count_plates(ET.fromstring(
                    z.read('Metadata/model_settings.config').decode('utf-8', 'ignore')))
    except Exception:
        plate_count = 1

    def _save(src, nice):
        if save_dir:
            try:
                os.makedirs(save_dir, exist_ok=True)
                shutil.copy(src, os.path.join(save_dir, nice))
            except Exception as e:
                print(f"WARN: could not save '{nice}' to output folder '{save_dir}': {e}")

    if plate_count <= 1:
        ok, err = convert_single_file(input_path, output_path, user_colors)
        if ok:
            _clear_stale_outputs(save_dir, base_name)
            _save(output_path, f"{base_name}_U1.3mf")
        return (ok, output_path if ok else None, None if ok else err)

    # Multi-plate: keep ALL plates in ONE file (Orca's plate grid). Preferred
    # output — one download, plates selectable in Orca.
    merged_ok, merged_err = convert_single_file(
        input_path, output_path, user_colors, merge_plates=True)
    if merged_ok:
        _clear_stale_outputs(save_dir, base_name)
        _save(output_path, f"{base_name}_U1.3mf")
        return (True, output_path, None)
    # A plate that can't fit (or other failure) drops us to the legacy path:
    # split into single-plate files, convert each, bundle a ZIP with a warning.
    print(f"WARN: one-file multi-plate convert failed ({merged_err}); "
          f"falling back to per-plate split.")

    import tempfile
    from split_plates import split_plates
    with tempfile.TemporaryDirectory() as td:
        ok, res = split_plates(input_path, td)
        if not ok:
            return (False, None, res)
        converted, errors = [], []
        for part in res:
            num = _plate_num(part)
            pout = os.path.join(td, f"part{num}_U1.3mf")
            s, e = convert_single_file(part, pout, user_colors)
            if s:
                converted.append((pout, num))
            else:
                errors.append(f"plate {num}: {e}")
        if not converted:
            return (False, None, "No plate could be converted. " + " | ".join(errors))
        _clear_stale_outputs(save_dir, base_name)
        zip_path = os.path.splitext(output_path)[0] + '.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for pout, num in converted:
                nice = f"{base_name}_plate{num}_U1.3mf"
                zf.write(pout, nice)
                _save(pout, nice)
        msg = None
        if errors:
            msg = f"{len(converted)} of {plate_count} plates converted. Skipped — {'; '.join(errors)}"
        return (True, zip_path, msg)


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

    # Also save print-ready file(s) to the configured output folder, if any.
    save_dir = (history_manager.get_settings() or {}).get('output_folder') or None

    # Multi-plate 3MFs are split into one file per plate and bundled as a ZIP.
    success, produced, message = convert_or_split_plates(
        input_path, output_path, user_colors, base_name=original_name, save_dir=save_dir)

    if success:
        produced_filename = os.path.basename(produced)
        is_zip = produced_filename.endswith('.zip')
        download_name = f"{original_name}_U1_plates.zip" if is_zip else f"{original_name}_U1.3mf"
        resp = {
            'download_url': f'/download/{produced_filename}',
            'download_name': download_name,
        }
        if message:
            resp['warning'] = message
        if save_dir:
            resp['saved_to'] = save_dir
        return jsonify(resp)
    else:
        return jsonify({'error': message}), 500


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

        # One deterministic output per model: overwrite any prior output
        # instead of creating _v2/_v3 duplicates of identical content.
        base_name = os.path.splitext(filename)[0]
        output_filename = f"{base_name}_U1.3mf"
        output_path = os.path.join(output_folder, output_filename)

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


@app.route('/dedup-outputs', methods=['POST'])
def dedup_outputs():
    """Remove byte-identical duplicate .3mf files from the Settings output
    folder, keeping one canonical copy per group (lossless). POST body
    {"apply": true} deletes; otherwise it's a dry run reporting what it would
    remove."""
    from dedup import dedup_folder
    settings = history_manager.get_settings()
    folder = settings.get('output_folder')
    if not folder or not os.path.isdir(folder):
        return jsonify({'error': f'Output folder not set or not found: {folder!r}'}), 400
    apply = bool((request.get_json(silent=True) or {}).get('apply', False))
    rep = dedup_folder(folder, apply=apply)
    return jsonify({
        'success': True,
        'applied': apply,
        'groups': rep['groups'],
        'removed_count': rep['removed_count'],
        'mb_freed': round(rep['bytes_freed'] / 1e6, 1),
        'kept': [os.path.basename(p['keep']) for p in rep['pairs']],
        'removed': [os.path.basename(r) for p in rep['pairs'] for r in p['removed']],
    })


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

        # One deterministic output per model: overwrite any prior output
        # instead of creating _v2/_v3 duplicates of identical content.
        base_name = os.path.splitext(filename)[0]
        output_filename = f"{base_name}_U1.3mf"
        output_path = os.path.join(output_folder, output_filename)

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

    # One deterministic output per model: overwrite any prior output
    # instead of creating _v2/_v3 duplicates of identical content.
    base_name = os.path.splitext(filename)[0]
    output_filename = f"{base_name}_U1.3mf"
    output_path = os.path.join(output_folder, output_filename)

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