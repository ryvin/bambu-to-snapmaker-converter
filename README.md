# Bambu Lab to Snapmaker U1 Converter

A web-based tool to convert Bambu Lab .3mf projects to Snapmaker U1 format, preserving multi-color painting and filament assignments.

## Features

- **Single File Conversion**: Upload and convert individual .3mf files with custom filament mapping
- **Batch Conversion**: Convert entire folders of files with automatic filament type detection
- **Auto-Center**: Automatically re-centers models from Bambu bed (256mm) to U1 bed (230mm)
- **Drop-to-Bed**: Fixes Z offset issues for proper bed adhesion
- **Unlimited Colors**: Supports any number of filaments (swap filament between colors)
- **Source Folder Monitoring**: Detects new files and converts them with one click
- **Conversion History**: Tracks converted files to skip duplicates
- **Tree Support Detection**: Automatically enables supports if original file had them
- **Docker Support**: Easy deployment with Docker Compose

## How It Works

### Single File Mode
1. Upload your Bambu Lab .3mf file
2. Review and adjust filament colors/types if needed
3. Click "Convert and Download"
4. Open the converted file in **Snapmaker Orca** for final slicing

### Batch Mode
1. Click "Batch Convert" tab
2. Select a folder with .3mf files
3. Review detected Bambu files (Snapmaker files are automatically skipped)
4. Click "Convert All" - files are saved to your configured output folder

### Source Folder Monitoring
1. Configure source and output folders in Settings
2. New Bambu files appear with a badge count
3. Click "Convert All New" to process them sequentially with progress display

## Installation

### Docker (Recommended)

```bash
git clone https://github.com/ryvin/bambu-to-snapmaker-converter.git
cd bambu-to-snapmaker-converter

# Edit docker-compose.yml to set your volume mounts
docker-compose up -d
```

The application will be available at `http://localhost:8090`

### Manual Installation

```bash
git clone https://github.com/ryvin/bambu-to-snapmaker-converter.git
cd bambu-to-snapmaker-converter

pip install flask
python app.py
```

The application will be available at `http://localhost:8080`

## Configuration

### Docker Volumes

Edit `docker-compose.yml` to map your local folders:

```yaml
volumes:
  - /path/to/downloads:/mnt/e/Downloads      # Source folder for new files
  - /path/to/converted:/mnt/e/3D/converted_u1 # Output folder
```

### Settings (in-app)

- **Output Folder**: Where converted files are saved
- **Source Folder**: Monitored for new Bambu files
- **Auto-detect new files**: Enable/disable source folder monitoring
- **Skip exact duplicates**: Skip files with matching MD5 hash

## Project Structure

```
bambu-to-snapmaker-converter/
├── app.py                    # Flask backend with all conversion logic
├── history.py                # Conversion history and settings manager
├── templates/
│   └── index.html            # Single-page frontend (Tailwind CSS)
├── uploads/                  # Temporary file storage (auto-cleaned)
├── u1_template.3mf           # U1 template without supports
├── u1_template_supports.3mf  # U1 template with tree supports
├── filament_types.3mf        # Available U1 filament profiles
├── Dockerfile
└── docker-compose.yml
```

## Technical Details

### Conversion Process

1. **Printer Profile**: Changes printer settings from Bambu Lab to Snapmaker U1
2. **Filament Mapping**: Auto-maps filament types (PLA→PLA, PETG→PETG-HF, etc.)
3. **Color Preservation**: Maintains all color painting data from the original
4. **Auto-Center**: Re-centers model X,Y from 128,128 (Bambu) to 115,115 (U1)
5. **Z Offset Fix**: Removes Z translation from part matrices for bed adhesion
6. **Support Detection**: Enables Tree Supports if original had supports enabled

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface |
| `/analyze` | POST | Analyze single file, return filaments |
| `/convert` | POST | Convert single file with custom colors |
| `/batch-analyze` | POST | Analyze multiple files |
| `/batch-convert` | POST | Convert all analyzed files |
| `/convert-file` | POST | Convert single file from source folder |
| `/check-new` | GET | List new files in source folder |
| `/settings` | GET/POST | Get or update settings |
| `/history` | GET | Get conversion history |

### File Cleanup

Uploaded files are automatically deleted after 8 hours.

## Limitations

- Converted files must be sliced in Snapmaker Orca before printing
- Some advanced Bambu-specific features may not transfer

## Contributing

Contributions are welcome! Feel free to:
- Report bugs
- Suggest features
- Submit pull requests

## License

MIT License - feel free to use, modify, and distribute.

## Acknowledgments

- Original project by [josuanbn](https://github.com/josuanbn/bl2u1)
- Snapmaker community for feedback and testing
