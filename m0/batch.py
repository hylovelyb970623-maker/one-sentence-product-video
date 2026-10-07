"""Sequential, resumable local video batches. Validation is the default."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
import uuid

import requests

from pipeline.config import load_env


class BatchError(RuntimeError):
    pass


def image_url(path):
    if not path.is_file() or not 0 < path.stat().st_size <= 12 * 1024 * 1024:
        raise BatchError('Each image must be an existing file no larger than 12MB')
    from PIL import Image
    try:
        with Image.open(path) as picture:
            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[picture.format]
            if getattr(picture, 'n_frames', 1) != 1 or min(picture.size) < 64 or picture.width * picture.height > 24_000_000:
                raise ValueError()
            picture.verify()
    except Exception:
        raise BatchError('Each image must be a valid static PNG, JPEG or WebP of at least 64px') from None
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def load_manifest(path):
    document = json.loads(path.read_text(encoding='utf-8'))
    products = document.get('products') if isinstance(document, dict) else None
    if not isinstance(products, list) or not 1 <= len(products) <= 100:
        raise BatchError('Manifest requires 1–100 products')
    prepared, identifiers = [], set()
    for product in products:
        if not isinstance(product, dict):
            raise BatchError('Each product must be an object')
        identifier = product.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in identifiers:
            raise BatchError('Product IDs must be unique, using 1–64 letters, digits, underscores or hyphens')
        identifiers.add(identifier)
        sentence = product.get('sentence', '')
        if not isinstance(sentence, str) or not 1 <= len(sentence.strip()) <= 160:
            raise BatchError('Each product requires 1–160 characters of factual description')
        body = {key: product[key] for key in ('sentence', 'price', 'size', 'orientation', 'persona_authorized') if key in product}
        if body.get('size', 'standard') not in {'standard', 'hd'} or body.get('orientation', 'vertical') not in {'vertical', 'horizontal'}:
            raise BatchError('Unsupported size or orientation')
        if 'image' not in product:
            raise BatchError('Each product requires a local image path')
        body['image'] = image_url(path.parent / product['image'])
        if product.get('persona'):
            if product.get('persona_authorized') is not True:
                raise BatchError('Persona images require explicit portrait authorization')
            body['persona'] = image_url(path.parent / product['persona'])
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        prepared.append((identifier, body, digest))
    return prepared


def write_state(path, state):
    temporary = path.with_suffix('.pending')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def request(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=60, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise BatchError('Connection interrupted. Rerun with the same state file to recover; do not create a new batch') from None
    if response.status_code not in {200, 202}:
        code = response.status_code
        response.close()
        raise BatchError(f'Local API returned HTTP {code}; inspect the local administration page')
    return response


def run_batch(prepared, base_url, token, state_path, output, wait_seconds=7200):
    parsed = urlparse(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise BatchError('Batch API must be a loopback HTTP URL without credentials or a path')
    if len(token) < 32:
        raise BatchError('Configure DIRECTOR_AGENT_TOKEN with at least 32 characters')
    base_url = base_url.rstrip('/')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix('.lock').open('a') as lock, requests.Session() as session:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BatchError('Another process owns this batch state') from None
        state = json.loads(state_path.read_text()) if state_path.exists() else {'version': 1, 'items': {}}
        if state.get('version') != 1 or not isinstance(state.get('items'), dict):
            raise BatchError('Unsupported batch state')
        session.trust_env = False
        session.headers['Authorization'] = 'Bearer ' + token
        for identifier, body, digest in prepared:
            item = state['items'].setdefault(identifier, {'request_key': uuid.uuid4().hex, 'digest': digest})
            if item.get('digest') != digest:
                raise BatchError('Manifest content changed for an existing ID; restore it or use a new ID for an intentional new generation')
            target = output / f'{identifier}.mp4'
            if target.is_file() and item.get('download_sha256') == hashlib.sha256(target.read_bytes()).hexdigest():
                print(f'{identifier}: already downloaded', flush=True)
                continue
            write_state(state_path, state)
            if not item.get('job_id'):
                result = request(session, 'POST', base_url + '/api/agent/submit', json=body,
                                 headers={'Idempotency-Key': item['request_key']}).json()
                job_id = result.get('job_id', '')
                if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
                    raise BatchError('Invalid job identity from local API')
                item['job_id'] = job_id
                write_state(state_path, state)
            job_id = item['job_id']
            if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
                raise BatchError('Invalid job identity in batch state')
            deadline = time.monotonic() + wait_seconds
            while True:
                status = request(session, 'GET', f'{base_url}/api/agent/jobs/{job_id}').json()
                if status.get('stage') == 'done':
                    break
                if status.get('stage') in {'error', 'attention'}:
                    raise BatchError('Generation stopped or requires remote-state confirmation; inspect the local administration page')
                if time.monotonic() >= deadline:
                    raise BatchError('Wait limit reached. Job remains active; resume with the same state file')
                time.sleep(3)
            temporary = target.with_suffix('.pending.mp4')
            checksum = hashlib.sha256()
            try:
                with request(session, 'GET', f'{base_url}/api/agent/jobs/{job_id}/video', stream=True) as response, temporary.open('wb') as stream:
                    os.chmod(temporary, 0o600)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        checksum.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except requests.RequestException:
                raise BatchError('Download interrupted; rerun with the same state file') from None
            if not temporary.stat().st_size:
                raise BatchError('Empty video response; generation was not repeated')
            temporary.replace(target)
            item['download_sha256'] = checksum.hexdigest()
            write_state(state_path, state)
            print(f'{identifier}: downloaded', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--submit', action='store_true', help='Submit real GPU/model work after validating all items')
    parser.add_argument('--url', default='http://127.0.0.1:8666')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--output', type=Path, default=Path('out/batch'))
    args = parser.parse_args()
    load_env()
    try:
        prepared = load_manifest(args.manifest)
        print(f'Validated {len(prepared)} product variants; generation requires --submit', flush=True)
        if args.submit:
            run_batch(prepared, args.url, os.environ.get('DIRECTOR_AGENT_TOKEN', ''),
                      args.state or args.manifest.with_name('batch-state.local.json'), args.output)
    except BatchError as error:
        print(str(error), flush=True)
        return 1
    except (ValueError, OSError, TypeError):
        print('Batch stopped. Check the manifest, local API configuration and state file; existing jobs were not cancelled.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
