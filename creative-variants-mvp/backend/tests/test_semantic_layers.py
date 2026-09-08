import httpx
import pytest
from app.config import settings
from app.models import Layer, LayerCategory, LayerType, Project
from app.services import semantic_layers

@pytest.mark.parametrize('entries', [[], [{'id': 0, 'category': 'unknown'}],
    [{'id': True, 'category': 'price'}], [{'id': 3, 'category': 'price'}],
    [{'id': 0, 'category': 'background'}], [None]])
def test_invalid_answers(entries):
    with pytest.raises((ValueError, TypeError)):
        semantic_layers.validate(entries, 1)

def test_duplicate_ids():
    with pytest.raises(ValueError):
        semantic_layers.validate([{'id': 0, 'category': 'price'}] * 2, 2)

@pytest.mark.parametrize('fails', [False, True])
def test_one_request_and_atomic_fallback(project, monkeypatch, fails):
    model = Project(**project)
    layers = [Layer(name='Subtítulo', type=LayerType.TEXT,
                    category=LayerCategory.SUBHEADLINE, content='$99', width=60, height=20)]
    monkeypatch.setattr(settings, 'enable_layer_vision', True)
    monkeypatch.setattr(settings, 'openai_api_key', 'fake')
    calls = []
    def post(self, url, **kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': 'invalid' if fails else
                '{"layers":[{"id":0,"category":"price"}]}'}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    warnings = semantic_layers.classify(model, layers)
    assert len(calls) == 1
    assert bool(warnings) == fails
    assert layers[0].category == (LayerCategory.SUBHEADLINE if fails else LayerCategory.PRICE)
    assert layers[0].type == LayerType.TEXT
    if not fails:
        assert layers[0].name == 'Precio'
        assert layers[0].font_weight == 'bold'
        assert layers[0].meta['classification']['provider'] == 'openai'

        assert semantic_layers.classify(model, layers) == []
        assert len(calls) == 1  # mismo KV/cajas: reutiliza el resultado
        layers[0].content = '$199'
        assert semantic_layers.classify(model, layers) == []
        assert len(calls) == 2  # cambiar el copy invalida la caché

@pytest.mark.parametrize('status', [429, 500])
def test_http_failure_preserves_layers(project, monkeypatch, status):
    model = Project(**project)
    layers = [Layer(name='Original', type=LayerType.IMAGE, category=LayerCategory.PRODUCT,
                    width=60, height=40)]
    before = layers[0].model_dump()
    monkeypatch.setattr(settings, 'enable_layer_vision', True)
    monkeypatch.setattr(settings, 'openai_api_key', 'fake')
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(status, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    warnings = semantic_layers.classify(model, layers)
    assert f"HTTP {status}" in warnings[0]
    assert layers[0].model_dump() == before
    assert len(calls) == 1
    assert 'layer_vision_cache' not in model.meta


def test_disabled_never_calls_http(project, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No debe consultar el proveedor')
    monkeypatch.setattr(httpx.Client, 'post', forbidden)
    monkeypatch.setattr(settings, 'enable_layer_vision', False)
    assert semantic_layers.classify(Project(**project), [
        Layer(name='Texto', type=LayerType.TEXT, category=LayerCategory.HEADLINE,
              width=50, height=20)]) == []


def test_invalid_second_box_does_not_apply_first(project, monkeypatch):
    model = Project(**project)
    layers = [
        Layer(name='Copy', type=LayerType.TEXT, category=LayerCategory.SUBHEADLINE,
              width=60, height=20),
        Layer(name='Objeto', type=LayerType.IMAGE, category=LayerCategory.PRODUCT,
              x=100, width=60, height=40),
    ]
    before = [layer.model_dump() for layer in layers]
    monkeypatch.setattr(settings, 'enable_layer_vision', True)
    monkeypatch.setattr(settings, 'openai_api_key', 'fake')
    def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content':
                '{"layers":[{"id":0,"category":"price"},{"id":1,"category":"invalid"}]}'}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    assert semantic_layers.classify(model, layers)
    assert [layer.model_dump() for layer in layers] == before
    assert 'layer_vision_cache' not in model.meta


def test_analysis_uses_vision_before_missing_logo_warning(project, monkeypatch):
    from app.providers.base import Detection
    from app.services import analysis, segmentation

    model = Project(**project)
    monkeypatch.setattr(settings, 'enable_layer_vision', True)
    monkeypatch.setattr(settings, 'openai_api_key', 'fake')
    monkeypatch.setattr(segmentation, 'detect_regions', lambda *a, **k: (
        [Detection(x=100, y=100, width=100, height=100, score=0.9)], []))
    monkeypatch.setattr(segmentation, 'refine_box', lambda *a, **k: None)
    monkeypatch.setattr(analysis, 'detect_faces', lambda *a, **k: [])
    def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': '{"layers":[{"id":0,"category":"logo"}]}'}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    layers, warnings, *_ = analysis.analyze_project(model, run_ocr=False)
    logo = next(layer for layer in layers if layer.category == LayerCategory.LOGO)
    assert logo.locked
    assert logo.extracted
    assert logo.src
    assert not any('No se identificó un logo' in warning for warning in warnings)
    assert model.meta['layer_vision_cache']
