from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


def generate_demo_inputs(output_dir: Path) -> tuple[Path, Path, Path]:
    """Create a small aligned RGB/DSM/DTM GeoTIFF set for learning and testing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    height = width = 256
    rows, columns = np.indices((height, width))
    dtm = (550.0 + rows * 0.003 + columns * 0.002).astype(np.float32)
    dsm = dtm.copy()

    rgb = np.zeros((3, height, width), dtype=np.uint8)
    rgb[0], rgb[1], rgb[2] = 184, 176, 156

    # Two paved corridors.
    rgb[:, 112:140, :] = np.array([115, 119, 122])[:, None, None]
    rgb[:, :, 42:62] = np.array([122, 125, 128])[:, None, None]

    # Raised building rooftops.
    buildings = [(25, 80, 70, 112, 5.5), (78, 150, 128, 205, 8.0), (155, 70, 216, 111, 4.2)]
    roof_colours = [(196, 105, 76), (205, 190, 170), (150, 105, 92)]
    for (r1, c1, r2, c2, added_height), colour in zip(buildings, roof_colours):
        rgb[:, r1:r2, c1:c2] = np.array(colour)[:, None, None]
        dsm[r1:r2, c1:c2] += added_height

    # Green raised vegetation patches.
    vegetation = [(35, 175, 67, 222), (165, 155, 225, 225)]
    for r1, c1, r2, c2 in vegetation:
        rgb[:, r1:r2, c1:c2] = np.array([52, 158, 70])[:, None, None]
        dsm[r1:r2, c1:c2] += 3.5

    transform = from_origin(365000.0, 2050000.0, 0.25, 0.25)
    crs = "EPSG:32643"
    rgb_path, dsm_path, dtm_path = [output_dir / name for name in ("demo_rgb.tif", "demo_dsm.tif", "demo_dtm.tif")]

    with rasterio.open(rgb_path, "w", driver="GTiff", width=width, height=height, count=3, dtype="uint8", crs=crs, transform=transform) as dataset:
        dataset.write(rgb)
    for path, array in ((dsm_path, dsm), (dtm_path, dtm)):
        with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=crs, transform=transform) as dataset:
            dataset.write(array, 1)
    return rgb_path, dsm_path, dtm_path


if __name__ == "__main__":
    destination = Path(__file__).resolve().parent / "generated"
    files = generate_demo_inputs(destination)
    print("Created Dharani demo inputs:")
    for file in files:
        print(file)
