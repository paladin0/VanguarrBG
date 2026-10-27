"""Book and game catalogue discovery with approval-gated requests."""
from __future__ import annotations

import json
import os
import asyncio
import secrets
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.core.db import SessionLocal
from app.core.models import AppSetting

router = APIRouter()
_lock = Lock()
DATA_FILE = Path(os.environ.get('CATALOGUE_REVIEW_FILE', '/data/catalogue-review.json'))
BULK_FILE = DATA_FILE.with_name('catalogue-bulk-job.json')
_bulk_step_lock = asyncio.Lock()
CACHE_DIR = DATA_FILE.parent / 'catalogue-cache'
CACHE_TTL_SECONDS = 1800
CACHE_KINDS = ('authors', 'books', 'platforms', 'retroarr_platforms', 'games')
_cache_refresh_lock = asyncio.Lock()

def _cache_path(kind: str):
    if kind not in CACHE_KINDS:
        raise HTTPException(400, 'Invalid catalogue cache')
    return CACHE_DIR / (kind + '.json')

def _cache_read(kind):
    try:
        obj = json.loads(_cache_path(kind).read_text(encoding='utf-8'))
        if isinstance(obj, dict) and isinstance(obj.get('items'), list):
            return obj
    except (OSError, ValueError):
        pass
    return None

def _cache_store(kind, items):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result = {'items': items, 'count': len(items),
              'updated_at': datetime.now(timezone.utc).isoformat()}
    path = _cache_path(kind)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    return result

def _cache_fresh(obj):
    try:
        dt = datetime.fromisoformat(obj['updated_at'])
        return (datetime.now(timezone.utc) - dt).total_seconds() < CACHE_TTL_SECONDS
    except (KeyError, ValueError, TypeError):
        return False

async def _cached_fetch(kind, fetcher, *, force=False):
    previous = _cache_read(kind)
    if previous and not force:
        return {**previous, 'cached': True}
    try:
        items = await fetcher()
        return {**_cache_store(kind, items), 'cached': False}
    except Exception as exc:
        if previous:
            return {**previous, 'cached': True, 'stale': True,
                    'sync_error': str(exc)[:180]}
        raise

@router.get('/api/catalogue/cache/status')
async def catalogue_cache_status():
    return {'collections': {name: {
        'count': len(obj['items']), 'updated_at': obj['updated_at'],
        'stale': not _cache_fresh(obj)} if (obj := _cache_read(name)) else
        {'count': 0, 'updated_at': None, 'stale': True}
        for name in CACHE_KINDS}}

async def _refresh_catalogues(*, force=False):
    if _cache_refresh_lock.locked():
        return {'state': 'already_running'}
    outcomes = {}
    async with _cache_refresh_lock:
        for name, fetcher in (
            ('authors', _fetch_authors), ('books', _fetch_books),
            ('retroarr_platforms', _fetch_retroarr_platforms),
            ('platforms', _fetch_igdb_platforms), ('games', _fetch_games)):
            old = _cache_read(name)
            if old and _cache_fresh(old) and not force:
                continue
            try:
                data = await fetcher()
                saved = _cache_store(name, data)
                outcomes[name] = {'count': saved['count'], 'ok': True}
            except Exception as exc:
                outcomes[name] = {'ok': False, 'error': str(exc)[:180],
                                  'previous_data_preserved': old is not None}
            await asyncio.sleep(.25)
    return {'state': 'completed', 'collections': outcomes}



def _require_write_token(x_catalogue_token: str | None = Header(default=None)):
    expected = catalogue_setting('CATALOGUE_WRITE_TOKEN').strip()
    if not expected:
        return  # Match Vanguarr's existing LAN settings behaviour when no approval token is configured.
    if not x_catalogue_token or not secrets.compare_digest(expected, x_catalogue_token):
        raise HTTPException(403, 'Catalogue write access denied')

@router.post('/api/catalogue/cache/refresh', dependencies=[Depends(_require_write_token)])
async def refresh_catalogue_cache():
    # Explicit refresh; GET endpoints remain read-only and show saved data.
    return await _refresh_catalogues(force=True)



# Runtime settings in the existing /data persistent volume. Environment values remain
# bootstrap defaults; saved settings override them without container recreation.
SETTINGS_FILE = Path(os.environ.get('CATALOGUE_SETTINGS_FILE', '/data/catalogue-settings.json'))
CONFIG_FIELDS = {
 'CATALOGUE_LAZYLIBRARIAN_URL': 'url', 'CATALOGUE_LAZYLIBRARIAN_KEY': 'secret',
 'CATALOGUE_RETROARR_URL': 'url', 'CATALOGUE_RETROARR_KEY': 'secret',
 'CATALOGUE_IGDB_CLIENT_ID': 'text', 'CATALOGUE_IGDB_CLIENT_SECRET': 'secret',
 'CATALOGUE_HUNTARR_URL': 'url', 'CATALOGUE_HUNTARR_TOKEN': 'secret',
 'CATALOGUE_WRITE_TOKEN': 'secret',
 'CATALOGUE_AUTO_HUNT': 'bool', 'CATALOGUE_ENABLE_GAME_IMPORT': 'bool',
 'CATALOGUE_HUNT_INTERVAL_MINUTES': 'int',
 'CATALOGUE_GAME_BATCH_SIZE': 'int',
 'CATALOGUE_DEFAULT_MIN_RATING': 'int', 'CATALOGUE_DEFAULT_MAX_PER_PLATFORM': 'int',
 'CATALOGUE_INCLUDE_UNRATED': 'bool',
 'CATALOGUE_WANT_EBOOK': 'bool', 'CATALOGUE_WANT_AUDIOBOOK': 'bool',
}

# Values used when a new installation has not yet saved the optional fields.
CATALOGUE_DEFAULTS = {
    'CATALOGUE_DEFAULT_MIN_RATING': '70',
    'CATALOGUE_DEFAULT_MAX_PER_PLATFORM': '250',
    'CATALOGUE_HUNT_INTERVAL_MINUTES': '1',
    'CATALOGUE_GAME_BATCH_SIZE': '5',
    'CATALOGUE_INCLUDE_UNRATED': 'false',
    'CATALOGUE_WANT_EBOOK': 'true',
    'CATALOGUE_WANT_AUDIOBOOK': 'true',
    'CATALOGUE_AUTO_HUNT': 'false',
    'CATALOGUE_ENABLE_GAME_IMPORT': 'false',
}

def _saved_settings():
    """Read catalogue settings from Vanguarr's existing app_settings database.

    Old V5/V6 /data JSON settings are used only as a migration source; DB wins.
    """
    with _lock:
        legacy = {}
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                legacy = {k: str(v) for k, v in raw.items() if k in CONFIG_FIELDS}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise HTTPException(500, 'Cannot read legacy catalogue settings')
        with SessionLocal() as session:
            rows = session.scalars(select(AppSetting).where(AppSetting.key.in_(list(CONFIG_FIELDS)))).all()
            saved = {row.key: row.value for row in rows}
        return {**legacy, **saved}

def catalogue_setting(name: str, default: str = '') -> str:
    saved = _saved_settings()
    if name in saved and str(saved[name] or '').strip():
        return str(saved[name])
    return os.environ.get(name) or CATALOGUE_DEFAULTS.get(name, default)

class CatalogueSettingsUpdate(BaseModel):
    settings: dict[str, str | int | bool]

@router.get('/api/catalogue/settings')
async def get_catalogue_settings():
    saved = _saved_settings()
    fields = {}
    for name, kind in CONFIG_FIELDS.items():
        value = str(saved.get(name) or os.getenv(name) or CATALOGUE_DEFAULTS.get(name, ''))
        fields[name] = {'type': kind, 'value': '' if kind == 'secret' else value,
                        'configured': bool(value)}
    return {'fields': fields}

@router.put('/api/catalogue/settings')
async def update_catalogue_settings(payload: CatalogueSettingsUpdate, request: Request):
    # Reject cross-origin browser writes; like Vanguarr core settings, LAN access is trusted.
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Cross-origin settings changes are not allowed")
    unexpected = set(payload.settings) - CONFIG_FIELDS.keys()
    if unexpected:
        raise HTTPException(400, 'Unknown configuration field')
    updated = _saved_settings()
    for name, raw in payload.settings.items():
        kind = CONFIG_FIELDS[name]
        value = str(raw).strip()
        if kind == 'secret' and not value:
            continue  # Blank password means keep current value.
        if kind == 'url' and value:
            parts = urlparse(value)
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
                raise HTTPException(400, f'Invalid URL for {name}')
            value = value.rstrip('/')
        if kind == 'int':
            if not value:
                # Optional number left blank: restore the documented default.
                value = CATALOGUE_DEFAULTS.get(name, '')
            try: number = int(value)
            except (TypeError, ValueError): raise HTTPException(400, f'Invalid number for {name}')
            if not (0 <= number <= 10000): raise HTTPException(400, f'Out of range: {name}')
            value = str(number)
        if kind == 'bool':
            if value.lower() not in ('true','false'): raise HTTPException(400, f'Invalid boolean for {name}')
            value = value.lower()
        updated[name] = value
    # Store alongside all existing Vanguarr settings in app_settings.
    with _lock:
        with SessionLocal.begin() as session:
            existing = {row.key: row for row in session.scalars(
                select(AppSetting).where(AppSetting.key.in_(list(CONFIG_FIELDS)))
            ).all()}
            for name, value in updated.items():
                row = existing.get(name)
                if row is None:
                    session.add(AppSetting(key=name, value=value))
                else:
                    row.value = value
    return {'saved': True}

@router.get('/catalogue/settings', response_class=HTMLResponse)
async def catalogue_settings_page(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'templates'))
    return templates.TemplateResponse(request, 'catalogue_settings.html', {'page_title': 'Books, Audiobooks & Games Settings', 'path': '/catalogue/settings'})



def _configured_url(env: str) -> str:
    value = catalogue_setting(env).strip().rstrip('/')
    if not value:
        raise HTTPException(503, f'{env} is not configured')
    parts = urlparse(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise HTTPException(400, f'Invalid {env}')
    return value


async def _json_get(url: str, *, headers: dict | None = None, params: dict | None = None):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code in (401, 403):
                raise HTTPException(502, f'Upstream authentication rejected (HTTP {response.status_code}); check the API key saved in Vanguarr Books & Games Settings')
            if response.status_code >= 400:
                raise HTTPException(502, f'Upstream returned HTTP {response.status_code}; check the configured service URL and API access')
            if response.is_redirect:
                raise HTTPException(502, f'Upstream redirected (HTTP {response.status_code}); check the configured URL')
            try:
                return response.json()
            except ValueError as exc:
                raise HTTPException(502, 'Upstream did not return JSON; check service URL and API authentication') from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f'Upstream connection failed: {type(exc).__name__}') from exc


def _unwrap_catalogue_records(data, *, label: str, candidates: tuple[str, ...]):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in candidates:
            if isinstance(data.get(key), list):
                return data[key]
        # LazyLibrarian can return a JSON error object with HTTP 200.
        # Do not echo any upstream body which may contain sensitive details.
        if any(k in data for k in ('error', 'Error', 'message', 'Message', 'status', 'Status')):
            raise HTTPException(502, f'{label} returned an error object; verify the API key in Vanguarr settings')
    raise HTTPException(502, f'Unexpected {label} response format; check the connection settings')


@router.get('/api/catalogue/connections/test')
async def catalogue_connections_test():
    """Read-only authenticated probes using the credentials Vanguarr actually has saved."""
    checks = {}
    try:
        author_data = await _json_get(
            _configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api',
            params={'cmd': 'getIndex', 'apikey': _lazy_key()},
        )
        authors = _unwrap_catalogue_records(author_data, label='LazyLibrarian',
                                             candidates=('authors', 'Authors', 'items', 'data'))
        checks['lazylibrarian'] = {'ok': True, 'count': len(authors)}
    except HTTPException as exc:
        checks['lazylibrarian'] = {'ok': False, 'error': str(exc.detail)}
    try:
        key = catalogue_setting('CATALOGUE_RETROARR_KEY').strip()
        if not key:
            raise HTTPException(503, 'RetroArr key not configured in Vanguarr')
        game_data = await _json_get(
            _configured_url('CATALOGUE_RETROARR_URL') + '/api/v3/game',
            headers={'X-Api-Key': key},
        )
        games = _unwrap_catalogue_records(game_data, label='RetroArr',
                                           candidates=('records', 'items', 'data'))
        checks['retroarr'] = {'ok': True, 'count': len(games)}
    except HTTPException as exc:
        checks['retroarr'] = {'ok': False, 'error': str(exc.detail)}
    return {'checks': checks, 'ok': all(x['ok'] for x in checks.values())}


def _lazy_key() -> str:
    key = catalogue_setting('CATALOGUE_LAZYLIBRARIAN_KEY', '').strip()
    if not key:
        raise HTTPException(503, 'LazyLibrarian key not configured')
    return key


@router.get('/catalogue', response_class=HTMLResponse)
async def catalogue_page(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'templates'))
    return templates.TemplateResponse(request, 'catalogue.html', {'page_title': 'Books & Games Catalogue', 'path': '/catalogue'})


async def _fetch_authors():
    data = await _json_get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api',
                           params={'cmd':'getIndex','apikey':_lazy_key()})
    records = _unwrap_catalogue_records(data, label='LazyLibrarian authors',
                                        candidates=('authors','Authors','items','data'))
    return [{'id': str(a.get('AuthorID','')), 'name':a.get('AuthorName',''),
             'status':a.get('Status',''), 'books':a.get('TotalBooks',0)}
            for a in records if isinstance(a,dict)]

async def _fetch_books():
    data = await _json_get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api',
                           params={'cmd':'getAllBooks','apikey':_lazy_key()})
    records = _unwrap_catalogue_records(data, label='LazyLibrarian books',
                                        candidates=('books','Books','items','data'))
    return [{'id':str(b.get('BookID','')), 'title':b.get('BookName',''),
             'author':b.get('AuthorName',''), 'ebook':b.get('Status',''),
             'audiobook':b.get('AudioStatus','')}
            for b in records if isinstance(b,dict)]

async def _fetch_games():
    key = catalogue_setting('CATALOGUE_RETROARR_KEY', '').strip()
    if not key:
        raise HTTPException(503,'RetroArr API key not configured')
    data = await _json_get(_configured_url('CATALOGUE_RETROARR_URL') + '/api/v3/game',
                           headers={'X-Api-Key':key})
    return _unwrap_catalogue_records(data, label='RetroArr games',
                                     candidates=('records','items','data'))

async def _fetch_retroarr_platforms():
    key = catalogue_setting('CATALOGUE_RETROARR_KEY', '').strip()
    if not key:
        raise HTTPException(503, 'RetroArr API key not configured')
    data = await _json_get(_configured_url('CATALOGUE_RETROARR_URL') + '/api/v3/platform',
                           headers={'X-Api-Key': key})
    records = _unwrap_catalogue_records(data, label='RetroArr platforms',
                                        candidates=('records', 'items', 'data'))
    return [{'id': int(p['id']), 'igdbPlatformId':p.get('igdbPlatformId'),
             'name':p.get('name',''), 'enabled':bool(p.get('enabled', False))}
            for p in records if isinstance(p,dict) and p.get('id') is not None]

@router.get('/api/catalogue/books/authors')
async def authors(force: bool = False):
    return await _cached_fetch('authors', _fetch_authors, force=force)

@router.get('/api/catalogue/books/titles')
async def books(limit: int = Query(250, ge=1, le=10000), force: bool = False):
    result = await _cached_fetch('books', _fetch_books, force=force)
    return {**result, 'items':result['items'][:limit]}

@router.get('/api/catalogue/games/library')
async def games_library(force: bool = False):
    result = await _cached_fetch('games', _fetch_games, force=force)
    return {**result, 'items':result['items'][:500]}

@router.get('/api/catalogue/games/retroarr-platforms')
async def retroarr_platforms(force: bool = False):
    return await _cached_fetch('retroarr_platforms', _fetch_retroarr_platforms, force=force)


async def _igdb_token():
    client_id = catalogue_setting('CATALOGUE_IGDB_CLIENT_ID', '').strip()
    secret = catalogue_setting('CATALOGUE_IGDB_CLIENT_SECRET', '').strip()
    if not client_id or not secret:
        raise HTTPException(503,'IGDB Twitch client credentials not configured')
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post('https://id.twitch.tv/oauth2/token', params={'client_id':client_id,'client_secret':secret,'grant_type':'client_credentials'})
            response.raise_for_status()
            return client_id,response.json()['access_token']
    except (httpx.HTTPError,KeyError,ValueError) as exc:
        raise HTTPException(502,'IGDB authentication failed') from exc


async def _igdb_query(endpoint, query):
    client_id,token = await _igdb_token()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post('https://api.igdb.com/v4/'+endpoint,headers={'Client-ID':client_id,'Authorization':'Bearer '+token},content=query)
            response.raise_for_status()
            data=response.json()
            if not isinstance(data,list):
                raise ValueError('unexpected IGDB result')
            return data
    except httpx.HTTPStatusError as exc:
        # Never leak the credentials or token; report the upstream status and
        # validation message so a bad query is actionable.
        upstream = exc.response
        detail = 'IGDB HTTP ' + str(upstream.status_code)
        if upstream.status_code == 400:
            try:
                issues = upstream.json()
                if isinstance(issues, list) and issues:
                    detail += ': ' + str(issues[0].get('details') or issues[0].get('title') or 'invalid query')[:180]
            except (ValueError, TypeError, AttributeError):
                pass
        raise HTTPException(502, detail) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, 'IGDB query failed (' + type(exc).__name__ + ')') from exc


async def _fetch_igdb_platforms():
    all_platforms = []
    for offset in range(0, 5000, 500):
        batch = await _igdb_query('platforms',
            f'fields id,name,abbreviation,category; sort name asc; limit 500; offset {offset};')
        all_platforms.extend(batch)
        if len(batch) < 500:
            break
    distinct = {int(p['id']):p for p in all_platforms if p.get('id')}
    return list(distinct.values())

@router.get('/api/catalogue/games/platforms')
async def platforms(force: bool = False):
    return await _cached_fetch('platforms', _fetch_igdb_platforms, force=force)


@router.get('/api/catalogue/games/discover')
async def discover(platform_id: int = Query(...,gt=0),min_rating: float | None = Query(None,ge=0,le=100),limit: int | None = Query(None,ge=1,le=2500),include_unrated: bool | None=None):
    if min_rating is None: min_rating = float(catalogue_setting('CATALOGUE_DEFAULT_MIN_RATING', '70'))
    if limit is None: limit = max(1, min(2500, int(catalogue_setting('CATALOGUE_DEFAULT_MAX_PER_PLATFORM', '250'))))
    if include_unrated is None: include_unrated = catalogue_setting('CATALOGUE_INCLUDE_UNRATED', 'false').lower() == 'true'
    # IGDB APICalypse rejects decimal literals in rating comparisons (70.0).
    # Round upward so fractional thresholds never admit a lower-rated game.
    rating_floor = math.ceil(float(min_rating))
    # IGDB category=0 excludes otherwise valid original releases in the current API.
    # Use platform membership plus the configured rating filter only.
    where=f'platforms = ({platform_id})'
    if include_unrated:
        where+=f' & (rating >= {rating_floor} | rating = null)'
    else:
        where+=f' & rating >= {rating_floor}'
    results=[]
    # IGDB enforces 500 per request; paginate and deduplicate game IDs.
    for offset in range(0,limit,500):
        qty=min(500,limit-offset)
        q=f'fields id,name,rating,rating_count,first_release_date,platforms; where {where}; sort rating desc; limit {qty}; offset {offset};'
        results.extend(await _igdb_query('games',q))
    items=[]
    for game in results:
        items.append({'id':game.get('id'),'title':game.get('name',''), 'rating':game.get('rating'), 'rating_count':game.get('rating_count',0), 'year':datetime.fromtimestamp(game['first_release_date'],tz=timezone.utc).year if game.get('first_release_date') else None,'platform_id':platform_id})
    return {'count':len(items),'items':items}


# Persistent platform-wide catalogue scanner. Queuing and approval remain explicit.
# One platform is scanned per worker step; a failed platform is recorded and can
# be retried in a new plan without discarding previously queued entries.
class BulkDiscoveryRequest(BaseModel):
    platform_ids: list[int] = Field(default_factory=list, max_length=1000)
    all_platforms: bool = False
    retroarr_supported_only: bool = True
    min_rating: float = Field(default=70, ge=0, le=100)
    max_per_platform: int = Field(default=250, ge=1, le=2500)
    include_unrated: bool = False
    approve_matching: bool = False


def _load_bulk():
    if not BULK_FILE.exists():
        return {'state': 'idle', 'platform_ids': [], 'next_index': 0, 'queued': 0, 'errors': []}
    try:
        data = json.loads(BULK_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    raise HTTPException(500, 'Cannot read bulk discovery state')


def _save_bulk(data):
    BULK_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = BULK_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(BULK_FILE)


@router.get('/api/catalogue/games/bulk')
async def bulk_status():
    with _lock:
        return _load_bulk()


@router.post('/api/catalogue/games/bulk', dependencies=[Depends(_require_write_token)])
async def start_bulk(req: BulkDiscoveryRequest):
    with _lock:
        active = _load_bulk()
        if active.get('state') == 'running':
            raise HTTPException(409, 'Stop the current bulk discovery first')
    if req.all_platforms:
        entries = (await platforms())['items']
        ids = sorted(set(int(p['id']) for p in entries if int(p.get('id') or 0) > 0))
    else:
        ids = sorted(set(i for i in req.platform_ids if i > 0))
    if req.retroarr_supported_only:
        retro = (await retroarr_platforms())['items']
        supported = {int(p['igdbPlatformId']) for p in retro
                     if p.get('enabled') and p.get('igdbPlatformId') is not None}
        ids = [i for i in ids if i in supported]

    if not ids:
        raise HTTPException(400, 'Choose at least one platform')
    job = {'state': 'running', 'platform_ids': ids, 'next_index': 0,
           'queued': 0, 'skipped': 0, 'errors': [], 'current_platform': None,
           'min_rating': req.min_rating, 'max_per_platform': req.max_per_platform,
           'include_unrated': req.include_unrated, 'approve_matching': req.approve_matching,
           'retroarr_supported_only': req.retroarr_supported_only,
           'started_at': datetime.now(timezone.utc).isoformat()}
    with _lock:
        _save_bulk(job)
    return job


@router.post('/api/catalogue/games/bulk/stop', dependencies=[Depends(_require_write_token)])
async def stop_bulk():
    with _lock:
        job = _load_bulk()
        job['state'] = 'stopped'
        _save_bulk(job)
    return job


async def _bulk_discovery_step():
    if _bulk_step_lock.locked():
        return
    async with _bulk_step_lock:
        with _lock:
            job = _load_bulk()
        if job.get('state') != 'running':
            return
        idx = int(job.get('next_index', 0))
        ids = job.get('platform_ids', [])
        if idx >= len(ids):
            with _lock:
                current = _load_bulk()
                if current.get('state') == 'running':
                    current['state'] = 'completed'
                    _save_bulk(current)
            return
        platform_id = int(ids[idx])
        try:
            response = await discover(platform_id=platform_id,
                min_rating=float(job['min_rating']), limit=int(job['max_per_platform']),
                include_unrated=bool(job['include_unrated']))
            fresh = 0
            duplicates = 0
            with _lock:
                current = _load_bulk()
                if current.get('state') != 'running' or int(current.get('next_index',0)) != idx:
                    return
                entries = _read_review()
                existing = {_item_key(item) for item in entries}
                for game in response['items']:
                    if not game.get('id') or not game.get('title'):
                        continue
                    rec = {'kind':'game', 'external_id':str(game['id']),
                           'title':game['title'], 'platform_id':platform_id,
                           'rating':game.get('rating'), 'requested_formats':[],
                           'state':'approved' if job['approve_matching'] else 'pending',
                           'attempts':0, 'last_error':'', 'processed_at':None}
                    key = _item_key(rec)
                    if key in existing:
                        duplicates += 1
                    else:
                        entries.append(rec)
                        existing.add(key)
                        fresh += 1
                _save_review(entries)
                current['queued'] = int(current.get('queued',0)) + fresh
                current['skipped'] = int(current.get('skipped',0)) + duplicates
                current['next_index'] = idx + 1
                current['current_platform'] = platform_id
                if current['next_index'] >= len(ids):
                    current['state'] = 'completed'
                _save_bulk(current)
        except Exception as exc:
            with _lock:
                current = _load_bulk()
                if current.get('state') == 'running' and current.get('next_index') == idx:
                    current['errors'] = (current.get('errors') or [])[-49:] + [
                        {'platform_id': platform_id, 'error': str(exc)[:160]}]
                    # Do not silently skip a platform that was never scanned.
                    # Pause the job at its current index for inspection/restart.
                    current['state'] = 'paused_error'
                    current['current_platform'] = platform_id
                    _save_bulk(current)


class ReviewItem(BaseModel):
    kind: str = Field(pattern='^(game|book|author)$')
    external_id: str = Field(min_length=1,max_length=100)
    title: str = Field(min_length=1,max_length=300)
    platform_id: int | None = None
    rating: float | None = Field(default=None,ge=0,le=100)
    requested_formats: list[str] = Field(default_factory=lambda: ['eBook', 'AudioBook'])


def _read_review():
    if not DATA_FILE.exists():
        return []
    try:
        content=json.loads(DATA_FILE.read_text(encoding='utf-8'))
        return content if isinstance(content,list) else []
    except (OSError,ValueError):
        raise HTTPException(500,'Could not read review queue')


@router.get('/api/catalogue/review')
async def review_list():
    with _lock:
        return {'items':_read_review()}


@router.post('/api/catalogue/review', dependencies=[Depends(_require_write_token)])
async def review_add(item: ReviewItem):
    with _lock:
        entries=_read_review()
        record=item.model_dump()
        record.update(state='pending', attempts=0, last_error='', processed_at=None)
        if not any(x.get('kind')==record['kind'] and x.get('external_id')==record['external_id'] and x.get('platform_id')==record['platform_id'] for x in entries):
            entries.append(record)
            DATA_FILE.parent.mkdir(parents=True,exist_ok=True)
            temp=DATA_FILE.with_suffix('.tmp')
            temp.write_text(json.dumps(entries,indent=2),encoding='utf-8')
            temp.replace(DATA_FILE)
        return {'count':len(entries)}


@router.delete('/api/catalogue/review', dependencies=[Depends(_require_write_token)])
async def review_clear():
    with _lock:
        DATA_FILE.parent.mkdir(parents=True,exist_ok=True)
        DATA_FILE.write_text('[]',encoding='utf-8')
    return {'count':0}


# Approval and execution are separate operations. Auto-hunting is opt-in and
# never happens on discovery or review queue creation.
class ApproveRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=500)


def _save_review(entries: list[dict]):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(entries, indent=2), encoding='utf-8')
    tmp.replace(DATA_FILE)


def _item_key(item: dict) -> str:
    return f"{item.get('kind')}:{item.get('external_id')}:{item.get('platform_id') or ''}"


@router.post('/api/catalogue/review/approve', dependencies=[Depends(_require_write_token)])
async def approve_review(request: ApproveRequest):
    if not request.keys:
        raise HTTPException(400, 'Select at least one item')
    keys = set(request.keys)
    with _lock:
        entries = _read_review()
        count = 0
        for item in entries:
            if _item_key(item) in keys and item.get('state', 'pending') == 'pending':
                item['state'] = 'approved'
                count += 1
        _save_review(entries)
    return {'approved': count}


@router.get('/api/catalogue/automation/status')
async def automation_status():
    with _lock:
        entries = _read_review()
    counts = {}
    for item in entries:
        status = item.get('state', 'pending')
        counts[status] = counts.get(status, 0) + 1
    return {'counts': counts, 'auto_enabled': catalogue_setting('CATALOGUE_AUTO_HUNT', 'false').lower() == 'true',
            'game_writes_enabled': catalogue_setting('CATALOGUE_ENABLE_GAME_IMPORT', 'false').lower() == 'true',
            'interval_minutes': int(catalogue_setting('CATALOGUE_HUNT_INTERVAL_MINUTES', '1')),
            'batch_size': int(catalogue_setting('CATALOGUE_GAME_BATCH_SIZE', '5'))}


async def _ll_command(cmd: str, **params):
    return await _json_get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api',
                           params={'apikey': _lazy_key(), 'cmd': cmd, **params})


async def _ll_command_result(cmd: str, **params):
    # LazyLibrarian's command endpoints often return plain text 'OK'.
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api',
                                        params={'apikey': _lazy_key(), 'cmd': cmd, **params})
            response.raise_for_status()
            result = response.text.strip().strip('"')
            if result != 'OK':
                raise ValueError(f'Unexpected response from {cmd}: {result[:120]}')
            return result
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f'{cmd} failed: {type(exc).__name__}') from exc


async def _retroarr_request(method: str, path: str, *, payload: dict | None = None):
    """Call exact routes verified in RetroArr.Api.V3 source; no redirects."""
    base = _configured_url('CATALOGUE_RETROARR_URL')
    key = catalogue_setting('CATALOGUE_RETROARR_KEY', '').strip()
    if not key:
        raise RuntimeError('CATALOGUE_RETROARR_KEY not configured')
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            response = await client.request(method, base + path,
                                            headers={'X-Api-Key': key}, json=payload)
            if response.status_code == 409:
                return {'duplicate': True}
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f'RetroArr {method} {path} failed: {type(exc).__name__}') from exc


async def _import_game(item: dict):
    """IGDB ID -> RetroArr platform -> create -> monitored. No automatic download.

    The queue's platform_id refers to IGDB, not RetroArr's internal platform ID.
    Requires a trusted local RetroArr instance and explicit opt-in.
    """
    if catalogue_setting('CATALOGUE_ENABLE_GAME_IMPORT', 'false').lower() != 'true':
        raise RuntimeError('Game writes disabled: set CATALOGUE_ENABLE_GAME_IMPORT=true after testing')
    igdb_id = int(item['external_id'])
    igdb_platform = int(item['platform_id'])
    if igdb_id <= 0 or igdb_platform <= 0:
        raise RuntimeError('A valid IGDB game ID and platform ID are required')

    # The configured platform catalogue is authoritative, even for empty libraries.
    # Multiple RetroArr systems may share a single IGDB mapping; favour the
    # primary enabled platform by its lowest internal ID, never an arbitrary game.
    configured = (await retroarr_platforms())['items']
    matches = sorted((p for p in configured if p.get('enabled')
                      and p.get('igdbPlatformId') is not None
                      and int(p['igdbPlatformId']) == igdb_platform), key=lambda p: p['id'])
    if not matches:
        raise RuntimeError(f'IGDB platform {igdb_platform} has no enabled RetroArr mapping')
    platform_id = int(matches[0]['id'])
    if platform_id <= 0:
        raise RuntimeError(f'IGDB platform {igdb_platform} has no RetroArr mapping')

    # RetroArr resolves title, release year and artwork using its own configured
    # metadata provider, avoiding user-supplied titles used as database payloads.
    metadata = await _retroarr_request('GET', f'/api/v3/game/lookup/igdb/{igdb_id}')
    if not isinstance(metadata, dict) or not metadata.get('title'):
        raise RuntimeError(f'IGDB game {igdb_id} not found by RetroArr')
    metadata['platformId'] = platform_id
    metadata['igdbId'] = igdb_id
    metadata['monitored'] = True
    metadata.pop('id', None)

    existing = await _retroarr_request('GET', '/api/v3/game')
    games = existing if isinstance(existing, list) else existing.get('records', []) if isinstance(existing, dict) else []
    existing_game = next((g for g in games if isinstance(g, dict)
                          and int(g.get('platformId') or 0) == platform_id
                          and int(g.get('igdbId') or 0) == igdb_id), None)
    if existing_game:
        game_id = int(existing_game['id'])
        result = 'Already in RetroArr'
    else:
        created = await _retroarr_request('POST', '/api/v3/game', payload=metadata)
        if created.get('duplicate'):
            raise RuntimeError('RetroArr detected a duplicate title; inspect the existing game before retrying')
        game_id = int(created.get('id') or 0)
        if game_id <= 0:
            raise RuntimeError('RetroArr created a game but did not return an ID; check the library before retrying')
        result = 'Game added to RetroArr'

    await _retroarr_request('PUT', f'/api/v3/games/{game_id}/monitored',
                            payload={'monitored': True})
    return game_id, f'{result}; monitoring enabled (game {game_id}, platform {platform_id})'


async def _submit_huntarr(item: dict, *, game_id: int | None = None):
    """Submit one approved record to Huntarr's persistent, bounded search queue."""
    base = _configured_url('CATALOGUE_HUNTARR_URL')
    token = catalogue_setting('CATALOGUE_HUNTARR_TOKEN', '').strip()
    if not token:
        raise RuntimeError('CATALOGUE_HUNTARR_TOKEN is required')
    payload = {
        'kind': item['kind'],
        'external_id': str(item['external_id']),
        'platform_id': int(item.get('platform_id') or 0),
        'formats': item.get('requested_formats') or ['eBook', 'AudioBook'],
    }
    if game_id is not None:
        payload['game_id'] = game_id
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            res = await client.post(base + '/api/media-hunt',
                                    headers={'X-Media-Hunt-Token': token}, json=payload)
            res.raise_for_status()
            return res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f'Huntarr queue submission failed: {type(exc).__name__}') from exc


async def _process_one(item: dict):
    kind = item.get('kind')
    if kind == 'game':
        game_id, message = await _import_game(item)
        # RetroArr's own monitored-game search route can dispatch a selected
        # release to its configured download client. This avoids depending on
        # Huntarr's optional media-hunt token and its independent scheduler.
        result = await _retroarr_request(
            'POST', f'/api/v3/games/{game_id}/search-now?autoDispatch=true')
        if not isinstance(result, dict):
            raise RuntimeError('RetroArr search returned an unexpected response')
        if result.get('error'):
            raise RuntimeError(f"RetroArr search error: {str(result['error'])[:180]}")
        if result.get('autoQueued') is True:
            return message + '; release queued to RetroArr download client'
        reason = str(result.get('autoDispatchSkipped') or 'no qualifying release found')[:180]
        return message + '; search completed, nothing queued (' + reason + ')'
    if kind == 'author':
        # Add author by ID or name; no bulk Wanted action while book catalogue
        # metadata is incomplete. Author metadata refresh is handled in LL.
        await _ll_command_result('addAuthor', name=item['title'])
        return 'Author submitted. Bibliography and Wanted statuses are controlled in LazyLibrarian.'
    if kind == 'book':
        current = await _ll_command('getBook', id=item['external_id'])
        records = current.get('book', []) if isinstance(current, dict) else []
        if isinstance(records, dict):
            records = [records]
        if not records:
            raise RuntimeError('Book not yet present in LazyLibrarian; refresh bibliography first')
        book = records[0]
        changed = []
        for fmt, field in [('eBook', 'Status'), ('AudioBook', 'AudioStatus')]:
            if fmt not in item.get('requested_formats', ['eBook','AudioBook']):
                continue
            current_status = str(book.get(field, '')).lower()
            if current_status in ('skipped', 'open'):
                await _ll_command_result('queueBook', id=item['external_id'], type=fmt)
                changed.append(fmt)
        await _submit_huntarr(item)
        return ('Marked Wanted: ' + ', '.join(changed) if changed else 'Already tracked or owned') + '; queued in Huntarr'
    raise RuntimeError('Unsupported request type')


async def process_approved(*, max_items: int = 10):
    if max_items < 1 or max_items > 50:
        raise ValueError('max_items must be 1..50')
    with _lock:
        entries = _read_review()
        selected = []
        # Unmapped items remain approved for future remapping. Do not burn
        # attempts on known unsupported IGDB platforms.
        platform_cache = _cache_read('retroarr_platforms') or {'items': []}
        supported = {int(p['igdbPlatformId']) for p in platform_cache['items']
                     if isinstance(p, dict) and p.get('enabled')
                     and p.get('igdbPlatformId') is not None}
        for item in entries:
            if item.get('state') != 'approved' or len(selected) >= max_items:
                continue
            if (item.get('kind') == 'game' and supported
                    and int(item.get('platform_id') or 0) not in supported):
                continue
            item['state'] = 'processing'
            selected.append(dict(item))
        _save_review(entries)
    results = []
    for item in selected:
        try:
            message = await _process_one(item)
            state, error = 'completed', ''
        except Exception as exc:
            # Safe retries require operator inspection rather than blind requeue.
            state, error, message = 'failed', str(exc)[:300], ''
        with _lock:
            entries = _read_review()
            for candidate in entries:
                if _item_key(candidate) == _item_key(item) and candidate.get('state') == 'processing':
                    candidate.update(state=state, last_error=error, result=message,
                                     attempts=int(candidate.get('attempts', 0))+1,
                                     processed_at=datetime.now(timezone.utc).isoformat())
                    break
            _save_review(entries)
        results.append({'key': _item_key(item), 'state': state, 'error': error, 'result': message})
        await asyncio.sleep(0.25)
    return {'processed': len(results), 'items': results}


@router.post('/api/catalogue/automation/run', dependencies=[Depends(_require_write_token)])
async def run_approved(max_items: int = Query(10, ge=1, le=50)):
    return await process_approved(max_items=max_items)


class RecommendRequest(BaseModel):
    preference: str = Field(min_length=3, max_length=2000)
    medium: str = Field(pattern='^(books|games)$')
    limit: int = Field(default=10, ge=1, le=20)


@router.post('/api/catalogue/recommend', dependencies=[Depends(_require_write_token)])
async def recommend(request: RecommendRequest):
    # Recommendations are text-only and MUST be approved separately.
    from app.api.llm import LLMClient
    from app.core.settings import get_settings
    llm = LLMClient(get_settings())
    system = ('You are a media recommendation librarian. Recommend real, existing titles '
              'and their creators only. Do not claim availability or ownership. '
              'Do not invent IDs. Return concise numbered suggestions.')
    # Include a bounded, non-sensitive slice of the synchronised catalogues.
    # This is context for suggestions only; no LLM outputs are automatically imported.
    context_lines = []
    if request.medium == 'books':
        authors_cache = _cache_read('authors') or {'items': []}
        books_cache = _cache_read('books') or {'items': []}
        context_lines.append(f'Saved catalogue: {len(authors_cache["items"])} authors; {len(books_cache["items"])} tracked books.')
        for book in books_cache['items'][:60]:
            context_lines.append(f'Tracked: {str(book.get("title") or "")[:90]} by {str(book.get("author") or "")[:60]}')
    else:
        games_cache = _cache_read('games') or {'items': []}
        platform_cache = _cache_read('retroarr_platforms') or {'items': []}
        context_lines.append(f'Saved RetroArr catalogue: {len(games_cache["items"])} games; {len(platform_cache["items"])} configured platforms.')
        for game in games_cache['items'][:60]:
            context_lines.append(f'Library game: {str(game.get("title") or "")[:100]}')
    prompt = (f'Offer up to {request.limit} {request.medium} based on this preference: '
              f'{request.preference}. For books give title and author; for games give title and platforms. '
              'Do not recommend duplicates already in the sample inventory; the sample is incomplete. '
              'All suggestions require review and metadata verification.\n'
              + '\n'.join(context_lines))
    try:
        text = await llm.generate_text(system_prompt=system, user_prompt=prompt,
                                       max_tokens=1000, temperature=0.4)
    except Exception as exc:
        raise HTTPException(502, f'LLM recommendation unavailable: {type(exc).__name__}') from exc
    return {'recommendations': text, 'review_only': True}


async def catalogue_auto_worker():
    # One persistent discovery step every 20 seconds, including after restart.
    # Approved imports run separately at the configured interval and are opt-in.
    elapsed = 0
    sync_elapsed = CACHE_TTL_SECONDS
    while True:
        try:
            if sync_elapsed >= CACHE_TTL_SECONDS:
                await _refresh_catalogues()
                sync_elapsed = 0
            await _bulk_discovery_step()
            mins = max(1, min(1440, int(catalogue_setting('CATALOGUE_HUNT_INTERVAL_MINUTES', '1'))))
            interval_seconds = mins * 60
            if elapsed >= interval_seconds:
                if (catalogue_setting('CATALOGUE_AUTO_HUNT', 'false').lower() == 'true'
                        and catalogue_setting('CATALOGUE_ENABLE_GAME_IMPORT', 'false').lower() == 'true'):
                    batch = max(1, min(20, int(catalogue_setting('CATALOGUE_GAME_BATCH_SIZE', '5'))))
                    await process_approved(max_items=batch)
                elapsed = 0
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Catalogue worker iteration failed')
        await asyncio.sleep(20)
        elapsed += 20
        sync_elapsed += 20

@router.get('/api/catalogue/games/capabilities')
async def retroarr_capabilities():
    """Read available API metadata, without guessing or invoking write routes."""
    base = _configured_url('CATALOGUE_RETROARR_URL')
    key = catalogue_setting('CATALOGUE_RETROARR_KEY', '').strip()
    if not key:
        raise HTTPException(503, 'RetroArr API key not configured')
    candidates = ['/swagger/v1/swagger.json', '/openapi/v1.json', '/swagger.json', '/openapi.json']
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        for path in candidates:
            try:
                response = await client.get(base + path, headers={'X-Api-Key': key})
                if response.status_code != 200:
                    continue
                data = response.json()
                if isinstance(data, dict) and isinstance(data.get('paths'), dict):
                    relevant = {}
                    for route, methods in data['paths'].items():
                        if any(token in route.lower() for token in ('game', 'monitor', 'search')):
                            relevant[route] = [method.upper() for method in methods if method.lower() in ('get','post','put','patch','delete')]
                    return {'schema_found': True, 'schema_path': path, 'routes': relevant}
            except (httpx.HTTPError, ValueError):
                continue
    return {'schema_found': False, 'routes': {}, 'message': 'No accessible OpenAPI schema; game write operations stay disabled'}
