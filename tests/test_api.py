import json

import rasterio
import app as app_module
from fastapi.testclient import TestClient
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject
from app import app
from demo.generate_demo_inputs import generate_demo_inputs
from processing import process_survey

client = TestClient(app)


def make_different_grid(source, destination, scale=0.5):
    with rasterio.open(source) as src:
        width = max(1, int(src.width * scale))
        height = max(1, int(src.height * scale))
        transform = from_bounds(*src.bounds, width, height)
        profile = src.profile.copy()
        profile.update(width=width, height=height, transform=transform)
        with rasterio.open(destination, 'w', **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=src.crs,
                    resampling=Resampling.bilinear,
                )
    return destination


def make_different_crs(source, destination, destination_crs='EPSG:4326'):
    with rasterio.open(source) as src:
        transform, width, height = calculate_default_transform(
            src.crs, destination_crs, src.width, src.height, *src.bounds
        )
        profile = src.profile.copy()
        profile.update(crs=destination_crs, transform=transform, width=width, height=height)
        with rasterio.open(destination, 'w', **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=destination_crs,
                    resampling=Resampling.bilinear,
                )
    return destination

def test_health():
    data = client.get('/api/health').json()
    assert data['status'] == 'ok'
    assert data['service'] == 'Dharani Prototype'
    assert data['team'] == 'PARSEK'

def test_project_identity():
    data = client.get('/api/project').json()
    assert data['name'] == 'Dharani'
    assert data['team'] == 'PARSEK'
    assert data['tagline'] == 'From aerial evidence to verified land records.'

def test_parcels_are_geojson():
    data = client.get('/api/parcels').json()
    assert data['type'] == 'FeatureCollection'
    assert len(data['features']) >= 4

def test_statistics():
    data = client.get('/api/statistics').json()
    assert data['parcel_count'] >= 4
    assert 'Residential' in data['area_by_land_use_class_m2']

def test_real_geotiff_processing(tmp_path):
    rgb, dsm, dtm = generate_demo_inputs(tmp_path / 'inputs')
    metadata = process_survey(rgb, dsm, dtm, tmp_path / 'output')
    assert metadata['status'] == 'completed'
    assert metadata['counts']['building_candidates'] >= 1
    assert metadata['risk_counts']['high'] + metadata['risk_counts']['medium'] >= 1
    assert sum(metadata['confidence_counts'].values()) >= 1
    assert (tmp_path / 'output' / 'rgb.png').exists()
    assert (tmp_path / 'output' / 'features.geojson').exists()
    features = json.loads((tmp_path / 'output' / 'features.geojson').read_text())['features']
    required = {
        'confidence', 'confidence_band', 'risk_score', 'risk_type',
        'risk_severity', 'risk_explanation', 'recommended_action', 'risk_disclaimer'
    }
    assert required.issubset(features[0]['properties'])


def test_different_raster_grids_are_automatically_aligned(tmp_path):
    rgb, dsm, dtm = generate_demo_inputs(tmp_path / 'inputs')
    dsm_resampled = make_different_grid(dsm, tmp_path / 'dsm_half_resolution.tif', 0.5)
    dtm_resampled = make_different_crs(dtm, tmp_path / 'dtm_epsg4326.tif')
    metadata = process_survey(rgb, dsm_resampled, dtm_resampled, tmp_path / 'output')
    assert metadata['status'] == 'completed'
    assert metadata['alignment']['auto_aligned'] is True
    assert metadata['alignment']['dsm_rgb_coverage_percent'] == 100.0
    assert metadata['alignment']['dtm_rgb_coverage_percent'] >= 99.0
    assert metadata['counts']['building_candidates'] >= 1


def test_five_stage_review_and_approval_workflow(tmp_path, monkeypatch):
    survey_root = tmp_path / 'surveys' / 'latest'
    monkeypatch.setattr(app_module, 'LATEST_SURVEY', survey_root)
    monkeypatch.setattr(app_module, 'LATEST_INPUT', survey_root / 'input')
    rgb, dsm, dtm = generate_demo_inputs(tmp_path / 'inputs')
    with rgb.open('rb') as rgb_file, dsm.open('rb') as dsm_file, dtm.open('rb') as dtm_file:
        response = client.post(
            '/api/surveys/upload',
            data={'project_name': 'Five Stage Workflow Test'},
            files={
                'rgb': ('rgb.tif', rgb_file, 'image/tiff'),
                'dsm': ('dsm.tif', dsm_file, 'image/tiff'),
                'dtm': ('dtm.tif', dtm_file, 'image/tiff'),
            },
        )
    assert response.status_code == 200
    queue = client.get('/api/surveys/latest/review-queue').json()['features']
    assert queue
    feature_id = queue[0]['properties']['feature_id']
    note_response = client.post(
        f'/api/surveys/latest/features/{feature_id}/field-notes',
        json={
            'officer': 'Test Officer', 'language': 'English',
            'note': 'Boundary checked on site.', 'latitude': 18.52, 'longitude': 73.85,
        },
    )
    assert note_response.status_code == 200
    review_response = client.patch(
        f'/api/surveys/latest/features/{feature_id}/review',
        json={'status': 'verified', 'reviewer': 'Test Administrator'},
    )
    assert review_response.status_code == 200
    approved = client.get('/api/surveys/latest/approved').json()['features']
    assert any(feature['properties']['feature_id'] == feature_id for feature in approved)
    qr_response = client.get(f'/api/surveys/latest/features/{feature_id}/qr')
    assert qr_response.status_code == 200
    assert qr_response.headers['content-type'] == 'image/png'
    assert client.get('/api/surveys/latest/export').status_code == 200
    audit = client.get('/api/surveys/latest/audit').json()
    assert [event['event'] for event in audit] == [
        'survey_processed', 'field_note_added', 'survey_feature_reviewed'
    ]
