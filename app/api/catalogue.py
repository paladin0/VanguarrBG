"""Book and game catalogue discovery with approval-gated requests."""
from __future__ import annotations

import json
import os
import asyncio
import secrets
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter()
_lock = Lock()
DATA_FILE = Path(os.environ.get('CATALOGUE_REVIEW_FILE', '/data/catalogue-review.json'))


def _require_write_token(x_catalogue_token: str | None = Header(default=None)):
    expected = catalogue_setting('CATALOGUE_WRITE_TOKEN').strip()
    if not expected:
        raise HTTPException(503, 'CATALOGUE_WRITE_TOKEN must be configured for mutations')
    if not x_catalogue_token or not secrets.compare_digest(expected, x_catalogue_token):
        raise HTTPException(403, 'Catalogue write access denied')


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
 'CATALOGUE_DEFAULT_MIN_RATING': 'int', 'CATALOGUE_DEFAULT_MAX_PER_PLATFORM': 'int',
 'CATALOGUE_INCLUDE_UNRATED': 'bool',
 'CATALOGUE_WANT_EBOOK': 'bool', 'CATALOGUE_WANT_AUDIOBOOK': 'bool',
}

def _saved_settings():
    with _lock:
        try:
            obj = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            return obj if isinstance(obj, dict) else {}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            raise HTTPException(500, 'Cannot read catalogue settings')

def catalogue_setting(name: str, default: str = '') -> str:
    obj = _saved_settings()
    if name in obj:
        return str(obj[name])
    return os.environ.get(name, default)

class CatalogueSettingsUpdate(BaseModel):
    settings: dict[str, str | int | bool]

@router.get('/api/catalogue/settings')
async def get_catalogue_settings():
    saved = _saved_settings()
    fields = {}
    for name, kind in CONFIG_FIELDS.items():
        value = str(saved.get(name, os.getenv(name, '')))
        fields[name] = {'type': kind, 'value': '' if kind == 'secret' else value,
                        'configured': bool(value)}
    return {'fields': fields}

@router.put('/api/catalogue/settings', dependencies=[Depends(_require_write_token)])
async def update_catalogue_settings(payload: CatalogueSettingsUpdate):
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
            try: number = int(value)
            except ValueError: raise HTTPException(400, f'Invalid number for {name}')
            if not (0 <= number <= 10000): raise HTTPException(400, f'Out of range: {name}')
            value = str(number)
        if kind == 'bool':
            if value.lower() not in ('true','false'): raise HTTPException(400, f'Invalid boolean for {name}')
            value = value.lower()
        updated[name] = value
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile('w', dir=SETTINGS_FILE.parent, delete=False, encoding='utf-8') as temp:
        os.chmod(temp.name, 0o600)
        json.dump(updated, temp, indent=2)
        temp_name = temp.name
    os.replace(temp_name, SETTINGS_FILE)
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
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f'Upstream connection failed: {type(exc).__name__}') from exc


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


@router.get('/api/catalogue/books/authors')
async def authors():
    data = await _json_get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api', params={'cmd':'getIndex','apikey':_lazy_key()})
    if not isinstance(data, list):
        raise HTTPException(502, 'Unexpected author format from LazyLibrarian')
    return {'count': len(data), 'items': [{'id': str(a.get('AuthorID','')), 'name':a.get('AuthorName',''), 'status':a.get('Status',''), 'books':a.get('TotalBooks',0)} for a in data if isinstance(a,dict)]}


@router.get('/api/catalogue/books/titles')
async def books(limit: int = Query(250, ge=1, le=1000)):
    data = await _json_get(_configured_url('CATALOGUE_LAZYLIBRARIAN_URL') + '/api', params={'cmd':'getAllBooks','apikey':_lazy_key()})
    if not isinstance(data, list):
        raise HTTPException(502, 'Unexpected book format from LazyLibrarian')
    return {'count':len(data), 'items':[{'id':str(b.get('BookID','')), 'title':b.get('BookName',''), 'author':b.get('AuthorName',''), 'ebook':b.get('Status',''), 'audiobook':b.get('AudioStatus','')} for b in data[:limit] if isinstance(b,dict)]}


@router.get('/api/catalogue/games/library')
async def games_library():
    base = _configured_url('CATALOGUE_RETROARR_URL')
    key = catalogue_setting('CATALOGUE_RETROARR_KEY', '').strip()
    if not key:
        raise HTTPException(503, 'RetroArr API key not configured')
    data = await _json_get(base + '/api/v3/game',headers={'X-Api-Key':key})
    items = data if isinstance(data,list) else data.get('records',data.get('items',[])) if isinstance(data,dict) else []
    if not isinstance(items,list):
        raise HTTPException(502,'Unrecognised RetroArr game list format')
    return {'count':len(items),'items':items[:500]}


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
    except (httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502,'IGDB query failed') from exc


@router.get('/api/catalogue/games/platforms')
async def platforms():
    # Pages of 500. Includes classic computers, handhelds and arcades.
    all_platforms=[]
    for offset in range(0, 1000, 500):
        batch=await _igdb_query('platforms',f'fields id,name,abbreviation,category; sort name asc; limit 500; offset {offset};')
        all_platforms.extend(batch)
        if len(batch)<500:
            break
    return {'count':len(all_platforms),'items':all_platforms}


@router.get('/api/catalogue/games/discover')
async def discover(platform_id: int = Query(...,gt=0),min_rating: float | None = Query(None,ge=0,le=100),limit: int | None = Query(None,ge=1,le=500),include_unrated: bool | None=None):
    if min_rating is None: min_rating = float(catalogue_setting('CATALOGUE_DEFAULT_MIN_RATING', '70'))
    if limit is None: limit = max(1, min(500, int(catalogue_setting('CATALOGUE_DEFAULT_MAX_PER_PLATFORM', '250'))))
    if include_unrated is None: include_unrated = catalogue_setting('CATALOGUE_INCLUDE_UNRATED', 'false').lower() == 'true'
    where=f'platforms = ({platform_id}) & category = 0'
    if include_unrated:
        where+=f' & (rating >= {min_rating} | rating = null)'
    else:
        where+=f' & rating >= {min_rating} & rating_count > 0'
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
            'interval_minutes': int(catalogue_setting('CATALOGUE_HUNT_INTERVAL_MINUTES', '360'))}


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

    platform = await _retroarr_request('GET', f'/api/v3/platform/by-igdb/{igdb_platform}')
    platform_id = int(platform.get('id') or 0)
    if platform_id <= 0:
        raise RuntimeError(f'IGDB platform {igdb_platform} has no RetroArr mapping')

    # RetroArr resolves title, release year and artwork using its own configured
    # metadata provider, avoiding user-supplied titles used as database payloads.
    metadata = await _retroarr_request('GET', f'/api/v3/game/lookup/igdb/{igdb_id}')
    if not isinstance(metadata, dict) or not metadata.get('title'):
        raise RuntimeError(f'IGDB game {igdb_id} not found by RetroArr')
    metadata['platformId'] = platform_id
    metadata['igdbId'] = igdb_id
    metadata['monitored'] = False
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
    # Deliberately do not issue /search-now?autoDispatch=true: selection is
    # review-only and a separate scheduler should control searching.
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
        await _submit_huntarr(item, game_id=game_id)
        return message + '; queued for Huntarr search'
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
        for item in entries:
            if item.get('state') == 'approved' and len(selected) < max_items:
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
    prompt = (f'Offer up to {request.limit} {request.medium} based on this preference: '
              f'{request.preference}. For books give title and author; for games give title and platforms.')
    try:
        text = await llm.generate_text(system_prompt=system, user_prompt=prompt,
                                       max_tokens=1000, temperature=0.4)
    except Exception as exc:
        raise HTTPException(502, f'LLM recommendation unavailable: {type(exc).__name__}') from exc
    return {'recommendations': text, 'review_only': True}


async def catalogue_auto_worker():
    # Start only when specifically enabled. Deliberately does NOT trigger a
    # global forceBookSearch; use LazyLibrarian's native rate-limited scheduler.
    while True:
        try:
            mins = max(15, min(1440, int(catalogue_setting('CATALOGUE_HUNT_INTERVAL_MINUTES', '360'))))
            if catalogue_setting('CATALOGUE_AUTO_HUNT', 'false').lower() == 'true':
                await process_approved(max_items=10)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Catalogue worker iteration failed')
        await asyncio.sleep(mins * 60)

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
