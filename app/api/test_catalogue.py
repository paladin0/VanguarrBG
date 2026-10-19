"""Unit tests for the review-only catalogue queue."""
import json

from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.api import catalogue


def test_review_queue_deduplicates_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, 'DATA_FILE', tmp_path / 'queue.json')
    monkeypatch.setenv('CATALOGUE_WRITE_TOKEN', 'unit-test-token')
    app = FastAPI()
    app.include_router(catalogue.router)
    client = TestClient(app)
    entry = {'kind':'game','external_id':'123','title':'Example game','platform_id':19,'rating':87}
    assert client.post('/api/catalogue/review', json=entry, headers={'X-Catalogue-Token':'unit-test-token'}).json()['count'] == 1
    assert client.post('/api/catalogue/review', json=entry, headers={'X-Catalogue-Token':'unit-test-token'}).json()['count'] == 1
    assert len(client.get('/api/catalogue/review').json()['items']) == 1
    assert client.delete('/api/catalogue/review', headers={'X-Catalogue-Token':'unit-test-token'}).json()['count'] == 0
    assert json.loads((tmp_path / 'queue.json').read_text()) == []


def test_review_rejects_invalid_rating(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, 'DATA_FILE', tmp_path / 'queue.json')
    monkeypatch.setenv('CATALOGUE_WRITE_TOKEN', 'unit-test-token')
    app = FastAPI()
    app.include_router(catalogue.router)
    response = TestClient(app).post('/api/catalogue/review',json={'kind':'game','external_id':'42','title':'Example','rating':110}, headers={'X-Catalogue-Token':'unit-test-token'})
    assert response.status_code == 422


def test_review_approval_needs_write_token(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, 'DATA_FILE', tmp_path / 'queue.json')
    monkeypatch.setenv('CATALOGUE_WRITE_TOKEN', 'unit-test-token')
    monkeypatch.setenv('CATALOGUE_WRITE_TOKEN', 'unit-test-token')
    client = TestClient(FastAPI())
    client.app.include_router(catalogue.router)
    entry = {'kind':'game','external_id':'123','title':'Example game','platform_id':19,'rating':87}
    assert client.post('/api/catalogue/review', json=entry).status_code == 403
    headers={'X-Catalogue-Token':'unit-test-token'}
    assert client.post('/api/catalogue/review', json=entry, headers=headers).status_code == 200
    key='game:123:19'
    assert client.post('/api/catalogue/review/approve',json={'keys':[key]},headers=headers).json()['approved']==1
    assert client.get('/api/catalogue/review').json()['items'][0]['state']=='approved'

import asyncio
import httpx


def test_submit_huntarr_with_game_id(monkeypatch):
    monkeypatch.setenv('CATALOGUE_HUNTARR_URL', 'http://huntarr:9706')
    monkeypatch.setenv('CATALOGUE_HUNTARR_TOKEN', 'test-shared-token')
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'queued': True})
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(catalogue.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    asyncio.run(catalogue._submit_huntarr({'kind': 'game', 'external_id': '234', 'platform_id': 12}, game_id=77))
    assert len(seen) == 1
    assert seen[0].url.path == '/api/media-hunt'
    assert seen[0].headers['X-Media-Hunt-Token'] == 'test-shared-token'
    assert json.loads(seen[0].content)['game_id'] == 77


def test_submit_huntarr_rejects_missing_token(monkeypatch):
    monkeypatch.setenv('CATALOGUE_HUNTARR_URL', 'http://huntarr:9706')
    monkeypatch.delenv('CATALOGUE_HUNTARR_TOKEN', raising=False)
    import pytest
    with pytest.raises(RuntimeError, match='CATALOGUE_HUNTARR_TOKEN'):
        asyncio.run(catalogue._submit_huntarr({'kind': 'book', 'external_id': '123'}))
