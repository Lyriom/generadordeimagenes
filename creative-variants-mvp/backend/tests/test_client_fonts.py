"""Fuentes reales del cliente: catálogo permanente y copia por proyecto."""
import hashlib

import pytest

from app.services import client_fonts, storage


def test_all_bundled_fonts_are_valid():
    clients = client_fonts.catalog()
    marcimex = next(item for item in clients if item['id'] == 'marcimex')
    assert len(marcimex['fonts']) == 100
    for font in marcimex['fonts']:
        payload, suffix = client_fonts.read_font('marcimex', font['id'])
        assert suffix in {'.ttf', '.otf'}
        assert hashlib.sha256(payload).hexdigest().startswith(font['id'])


def test_apply_persist_and_delete_project_keeps_catalog(client, project):
    catalog = client.get('/projects/font-library/catalog')
    assert catalog.status_code == 200
    fonts = catalog.json()[0]['fonts']
    regular = next(f['id'] for f in fonts if f['name'] == 'Zetafonts - CocoSharp Regular')
    bold = next(f['id'] for f in fonts if f['name'] == 'Zetafonts - CocoSharp Bold')
    project_id = project['project_id']
    response = client.post(f'/projects/{project_id}/references/font', data={
        'client_id': 'marcimex', 'saved_font': regular, 'saved_font_bold': bold,
    })
    assert response.status_code == 200, response.text
    saved = storage.load_project(project_id)
    assert saved.meta['client_fonts']['client_id'] == 'marcimex'
    for field, font_id in [('font', regular), ('font_bold', bold)]:
        payload, _ = client_fonts.read_font('marcimex', font_id)
        assert storage.abs_path(project_id, getattr(saved.references, field)).read_bytes() == payload
    response = client.post(f'/projects/{project_id}/references/font', data={
        'client_id': 'marcimex', 'saved_font': regular,
    })
    assert response.status_code == 200
    assert storage.load_project(project_id).references.font_bold is None
    assert client.delete(f'/projects/{project_id}').status_code == 200
    assert client.get('/projects/font-library/catalog').json() == catalog.json()
    assert client_fonts.read_font('marcimex', regular)[0]


@pytest.mark.parametrize('client_id,font_id', [('../marcimex', 'fake'), ('marcimex', '../../etc/passwd')])
def test_unknown_or_path_ids_leave_project_unchanged(client, project, client_id, font_id):
    before = storage.load_project(project['project_id']).model_dump()
    response = client.post(f"/projects/{project['project_id']}/references/font", data={
        'client_id': client_id, 'saved_font': font_id,
    })
    assert response.status_code == 400
    assert storage.load_project(project['project_id']).model_dump() == before
