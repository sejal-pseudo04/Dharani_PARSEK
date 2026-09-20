# Dharani Phase 2

**Team:** PARSEK  
**Tagline:** *From aerial evidence to verified land records.*  
**Problem statement:** SIH 2026 PS 26012

This version adds the mandatory drone-input workflow to the working Phase 1 review dashboard.

All five workflow sections are functional: Drone Input, Processing, Map Workspace, Field Verification and Approved Records. Reviews of uploaded features persist in GeoJSON, field notes carry GPS coordinates and language, verified records receive working QR codes, and processing/review actions are recorded in an audit trail.

## What is real in Phase 2

- Upload of RGB orthomosaic, DSM and DTM GeoTIFF files
- CRS and geographic-overlap validation
- Automatic DSM/DTM reprojection and resampling onto the RGB grid when CRS, size, resolution or pixel alignment differs
- Real `nDSM = DSM - DTM` calculation
- Real VDVI calculation from RGB bands
- Building, vegetation and surface-corridor baseline masks
- Raster-to-GeoJSON vector conversion
- Drone-image, nDSM, VDVI and classification overlays
- Clickable extracted candidates with evidence and confidence
- Per-feature confidence calculated from height, edge, spectral and shadow evidence
- Uploaded-feature screening risks with type, severity, explanation and recommended action
- Confidence and Risk map modes that recolour the uploaded GeoJSON vectors
- Existing review and GPS field-note workflow

## Honest limitation

The included extraction is a real geospatial baseline based on height, colour and geometry rules. It does not claim to run trained DeepLabV3+ or D-LinkNet checkpoints. Those models require suitable aerial training data and compatible `.pth` weights. Dharani already provides the input/output contract where those model adapters will be added.

Risk results are screening alerts, not legal conclusions. The current pipeline can flag patterns such as an elevated structure beside a detected access corridor, vegetation touching a corridor, weak corridor continuity or incomplete geometry at the survey edge. A legal parcel-overlap or encroachment finding requires an authoritative cadastral parcel or approved-road reference layer.

The three inputs no longer need identical width, height, CRS, resolution or pixel origin. Dharani uses the RGB orthomosaic as the reference grid and aligns the DSM and DTM automatically. The files must still describe the same geographic location; unrelated surveys are rejected.

## Required software

- Windows 10 or 11
- Python 3.12
- VS Code
- Internet connection for installation and OpenStreetMap tiles

Do not use Python 3.14 for this project. On a computer containing multiple versions, always create the environment with `py -3.12`.

## Step 1: Open the project

1. Extract `Dharani_PARSEK_Phase2.zip`.
2. Open VS Code.
3. Select **File > Open Folder**.
4. Open the `dharani-phase2` folder.
5. Select **Terminal > New Terminal**.

## Step 2: Confirm Python 3.12

```powershell
py -3.12 --version
```

The response should begin with `Python 3.12`.

## Step 3: Create the virtual environment

```powershell
py -3.12 -m venv .venv
```

Activate it:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

The terminal should now begin with `(.venv)`.

## Step 4: Install the packages

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Rasterio and OpenCV are larger than the Phase 1 packages, so installation can take several minutes.

## Step 5: Run the tests

```powershell
python -m pytest -q
```

Expected result:

```text
7 passed
```

The fifth test creates real GeoTIFFs, processes them and checks the output layers.

## Step 6: Generate the learning dataset

```powershell
python -m demo.generate_demo_inputs
```

The command creates:

```text
demo/generated/demo_rgb.tif
demo/generated/demo_dsm.tif
demo/generated/demo_dtm.tif
```

These are aligned, georeferenced files designed for learning the complete upload workflow.

## Step 7: Start Dharani

```powershell
python -m uvicorn app:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

Keep this terminal running.

## Step 8: Upload the mandatory inputs

In the **Drone survey input** section:

1. Keep or change the project name.
2. For RGB, choose `demo/generated/demo_rgb.tif`.
3. For DSM, choose `demo/generated/demo_dsm.tif`.
4. For DTM, choose `demo/generated/demo_dtm.tif`.
5. Select **Validate and process survey**.
6. Wait until the status says **Processing complete**.

The map will move to the uploaded survey and show:

- Uploaded RGB drone image
- Extracted vector candidates
- Layer checkboxes for classification, nDSM and VDVI
- Candidate evidence and confidence when a feature is selected

## Step 9: Inspect the layers

Use **Visible layers** to toggle:

- Uploaded drone image
- Extracted class raster
- nDSM height layer
- VDVI vegetation layer
- Extracted vector candidates
- Phase 1 demo parcels

The Phase 1 parcels remain available so the human-verification workflow can still be demonstrated.

## Input rules for real survey data

All three inputs must:

- Use `.tif` or `.tiff`
- Contain a CRS
- Have the same width and height
- Have the same pixel size
- Have the same geographic bounds and alignment
- Use RGB bands 1, 2 and 3 in the orthomosaic
- Store DSM and DTM heights in compatible units

If the files do not align, Dharani stops and explains the validation error instead of producing a misleading map.

## Project files

| File | Purpose |
|---|---|
| `app.py` | APIs, uploads, review actions and file serving |
| `processing.py` | Raster validation, nDSM, VDVI, masks and vectorization |
| `demo/generate_demo_inputs.py` | Generates learning GeoTIFFs |
| `data/parcels.geojson` | Phase 1 review demonstration data |
| `static/index.html` | Upload and map workspace structure |
| `static/styles.css` | Dharani interface styling |
| `static/app.js` | Upload, layers, map interaction and reviews |
| `tests/test_api.py` | API and real-raster processing tests |

## Stopping and restarting

Stop the server with `Ctrl+C`.

On a later day, open the folder and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
python -m uvicorn app:app --reload
```

You do not need to reinstall packages unless `requirements.txt` changes.

## Next engineering phase

1. Collect and label aerial training imagery.
2. Fine-tune DeepLabV3+ for buildings, land cover and boundary evidence.
3. Train or integrate D-LinkNet for road extraction.
4. Add model checkpoint configuration and GPU inference.
5. Replace baseline candidate masks with calibrated model probabilities.
6. Add parcel vertex editing, topology checks and legacy-map comparison.
7. Add offline field assignments, voice capture and QR generation.
