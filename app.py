import os
import zipfile
import shutil
import re
import json
import uuid
import time
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, send_file, jsonify
from history import HistoryManager

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

    except Exception as e:
        print(f"Error parsing filaments: {e}")
    return filaments


# U1 bed dimensions
U1_BED_SIZE = 230  # mm
U1_BED_CENTER = U1_BED_SIZE / 2  # 115mm


def recenter_model_transform(transform_str):
    """
    Parse a 3x4 transform matrix string and re-center X,Y to U1 bed center.
    Transform format: "m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz"
    Returns the modified transform string.
    """
    parts = transform_str.split()
    if len(parts) != 12:
        return transform_str  # Don't modify if unexpected format

    try:
        # Parse the 3x4 matrix
        # First 9 values are the 3x3 rotation/scale matrix
        # Last 3 values are translation (tx, ty, tz)
        values = [float(p) for p in parts]

        # Set X,Y to U1 bed center, keep Z as-is
        values[9] = U1_BED_CENTER   # tx = 115
        values[10] = U1_BED_CENTER  # ty = 115
        # values[11] stays as-is (tz)

        return ' '.join(str(v) for v in values)
    except (ValueError, IndexError):
        return transform_str  # Return original if parsing fails


def fix_part_matrix_z_offset(matrix_str):
    """
    Parse a 4x4 matrix string and remove any Z offset.
    Matrix format: "m00 m01 m02 m03 m10 m11 m12 m13 m20 m21 m22 m23 m30 m31 m32 m33"
    The Z offset is typically at position m23 (index 11).
    Returns the modified matrix string.
    """
    parts = matrix_str.split()
    if len(parts) != 16:
        return matrix_str  # Don't modify if unexpected format

    try:
        values = [float(p) for p in parts]
        # Position 11 is m23 (Z translation in 4x4 matrix)
        # Set it to 0 to remove Z offset
        values[11] = 0.0
        return ' '.join(str(int(v) if v == int(v) else v) for v in values)
    except (ValueError, IndexError):
        return matrix_str


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
    # 1. Copy the original file to start
    shutil.copy(input_path, output_path)

    # 2. Read original file's project settings first to determine template
    try:
        with zipfile.ZipFile(input_path, 'r') as z_orig:
            original_project_settings = json.loads(z_orig.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return (False, f'Could not read original project settings: {e}')

    # Determine which template to use based on support settings
    different_settings = original_project_settings.get('different_settings_to_system', [])
    has_support = any('enable_support' in s for s in different_settings if s)
    if has_support:
        template_file = 'u1_template_supports.3mf'
    else:
        template_file = 'u1_template.3mf'

    # 3. Read U1 Template's project settings
    try:
        with zipfile.ZipFile(template_file, 'r') as z_templ:
            u1_project_settings_json = json.loads(z_templ.read('Metadata/project_settings.config').decode('utf-8'))
    except Exception as e:
        return (False, f'U1 Template ({template_file}) not found on server: {e}')

    # 4. Process the 3MF archive
    temp_zip = output_path + ".temp"

    try:
        with zipfile.ZipFile(output_path, 'r') as zin:
            with zipfile.ZipFile(temp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                # Get the original slice_info.config to modify it
                slice_info_content = zin.read('Metadata/slice_info.config')

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

                # We iterate through the original list to preserve order
                for original_fil in original_filaments:
                    original_id = original_fil['id']
                    if original_id in user_colors:
                        # Find the corresponding node in the XML
                        node_to_update = filaments_parent.find(f".//filament[@id='{original_id}']")
                        if node_to_update is not None:
                            new_conf = user_colors[original_id]

                            # Store the mapping before changing
                            id_mapping[original_id] = str(new_id_counter)

                            # Re-map ID to be sequential (1-based)
                            node_to_update.set('id', str(new_id_counter))

                            # Update color and type
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

                # Find all extruder metadata tags and update them
                for metadata in model_root.findall('.//metadata[@key="extruder"]'):
                    old_extruder = metadata.get('value')
                    if old_extruder in id_mapping:
                        metadata.set('value', id_mapping[old_extruder])

                # Fix Z offset in part matrices (drop to bed)
                for metadata in model_root.findall('.//metadata[@key="matrix"]'):
                    old_matrix = metadata.get('value')
                    if old_matrix:
                        metadata.set('value', fix_part_matrix_z_offset(old_matrix))

                modified_model_settings = ET.tostring(model_root, encoding='utf-8', xml_declaration=True)
                # --- End Model Settings Modification ---

                # --- Start 3D Model Transform Modification (Auto-center) ---
                # Re-center model X,Y to U1 bed center (115, 115)
                model_3d_content = None
                modified_3d_model = None
                if '3D/3dmodel.model' in zin.namelist():
                    model_3d_content = zin.read('3D/3dmodel.model').decode('utf-8')
                    # Parse with namespace handling
                    # Register namespaces to preserve them in output
                    namespaces = {
                        '': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02',
                        'p': 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06',
                        'BambuStudio': 'http://schemas.bambulab.com/package/2021'
                    }
                    for prefix, uri in namespaces.items():
                        ET.register_namespace(prefix, uri)

                    model_3d_root = ET.fromstring(model_3d_content)

                    # Find all item elements in the build section and update transforms
                    # Need to use namespace-aware search
                    ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
                    for item in model_3d_root.findall('.//m:item', ns):
                        transform = item.get('transform')
                        if transform:
                            item.set('transform', recenter_model_transform(transform))

                    # Also check without namespace (some files may not use it)
                    for item in model_3d_root.findall('.//item'):
                        transform = item.get('transform')
                        if transform:
                            item.set('transform', recenter_model_transform(transform))

                    modified_3d_model = ET.tostring(model_3d_root, encoding='utf-8', xml_declaration=True)
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

        shutil.move(temp_zip, output_path)
        return (True, None)

    except Exception as e:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
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