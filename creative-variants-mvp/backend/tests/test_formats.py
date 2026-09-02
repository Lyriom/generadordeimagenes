"""Catálogo de plataformas, presets y metadatos de área segura."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import GenerateRequest
from app.models.formats import FORMAT_PRESETS, SUPPORTED_FORMATS, format_safe_area


def test_catalog_has_distinct_presets_for_same_pixel_size():
    assert SUPPORTED_FORMATS["meta_stories"] == (1080, 1920)
    assert SUPPORTED_FORMATS["meta_reels"] == (1080, 1920)
    assert SUPPORTED_FORMATS["youtube_video_vertical"] == (1080, 1920)
    assert format_safe_area("meta_stories") != format_safe_area("meta_reels")
    assert format_safe_area("meta_reels")["bottom"] == 0.35


def test_catalog_covers_meta_google_and_youtube():
    platforms = {item["platform"] for item in FORMAT_PRESETS.values()}
    assert {"Meta", "Google Ads", "YouTube"} <= platforms
    assert FORMAT_PRESETS["google_search_square"]["width"] == 1200
    assert FORMAT_PRESETS["youtube_thumbnail"]["height"] == 720
    assert all(item.get("last_verified") for item in FORMAT_PRESETS.values())


def test_capabilities_exposes_detailed_catalog(client: TestClient):
    response = client.get("/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    by_id = {item["id"]: item for item in body["format_catalog"]}
    assert by_id["meta_reels"]["safe_area"]["bottom"] == 0.35
    assert by_id["google_pmax_vertical"]["ratio"] == "4:5"
    assert by_id["youtube_video_landscape"]["media_type"] == "video_frame"


def test_generation_request_accepts_presets_and_keeps_legacy_aliases():
    modern = GenerateRequest(formats=["meta_feed_4_5", "youtube_thumbnail"])
    assert modern.formats == ["meta_feed_4_5", "youtube_thumbnail"]
    legacy = GenerateRequest(formats=["1080x1080"])
    assert legacy.formats == ["1080x1080"]
