"""Read-only installation checks. Never submits a generation or calls an LLM."""
import argparse
import json
import os
import sys
from urllib.parse import urlparse

from pipeline.config import load_env


def check(online=False):
    load_env()
    results = []

    def record(name, ready, detail):
        results.append({'check': name, 'status': 'ok' if ready else 'error', 'detail': detail})

    record('python', sys.version_info >= (3, 11), 'Requires Python 3.11 or newer')
    try:
        from pipeline import render
        render.preflight()
        record('render', True, 'FFmpeg and Chinese font available')
    except Exception:
        record('render', False, 'Install dependencies and a CJK font; optionally set FFMPEG_BINARY / CHINESE_FONT')
    workflow = None
    try:
        from pipeline.comfy_client import build_ref2va
        from pipeline.workflows import output_prefix
        workflow = build_ref2va('preflight only', 'product.png', ref_person_image_name='person.png', ref_video_file='continuity.mp4')
        output_prefix(workflow, 'director/preflight')
        record('workflow', True, 'Ref2VA API template and node mapping load successfully')
    except Exception:
        record('workflow', False, 'Check API JSON and DIRECTOR_WORKFLOW_CONFIG node mapping')
    base = os.environ.get('LLM_BASE_URL') or os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
    local = urlparse(base).hostname in {'localhost', '127.0.0.1', '::1'}
    ready = bool(os.environ.get('LLM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY') or (local and os.environ.get('LLM_MODEL')))
    record('planner', ready, 'Text model configured; credentials and model availability are not verified' if ready else 'Configure LLM_BASE_URL / LLM_MODEL / LLM_API_KEY or DEEPSEEK_API_KEY')
    results.append({'check': 'vision', 'status': 'ok' if os.environ.get('VISION_API_KEY') and os.environ.get('VISION_MODEL') else 'optional',
                    'detail': 'Image recognition and visual checks require a configured vision model'})
    token = os.environ.get('DIRECTOR_AGENT_TOKEN', '')
    results.append({'check': 'agent', 'status': ('ok' if len(token) >= 32 else 'error') if token else 'optional',
                    'detail': 'Agent API requires a separate token of at least 32 characters'})
    if online and workflow:
        try:
            from pipeline.comfy_client import ComfyUI
            client = ComfyUI.from_env()
            client.check()
            response = client.session.get(f'{client.base}/object_info', timeout=30)
            response.raise_for_status()
            available = response.json()
            missing = sorted({node['class_type'] for node in workflow.values()} - available.keys())
            record('comfy_nodes', not missing, 'Required nodes available' if not missing else 'Required workflow nodes are missing; compare template with ComfyUI object_info')
            missing_models = False
            for node in workflow.values():
                inputs = available.get(node['class_type'], {}).get('input', {})
                for key, value in node['inputs'].items():
                    if key not in {'unet_name', 'clip_name', 'vae_name', 'lora_name'} or value in (None, 'None', ''):
                        continue
                    schema = (inputs.get('required', {}) | inputs.get('optional', {})).get(key)
                    if schema and isinstance(schema[0], list) and value not in schema[0]:
                        missing_models = True
            record('comfy_models', not missing_models, 'Model selector names checked; weight files and licenses are not verified' if not missing_models else 'Template references unavailable model filenames; install authorized weights or update your local template')
            results.append({'check': 'queue', 'status': 'ok', 'detail': f'{client.queue_size()} remote jobs; none submitted or cancelled'})
            client.session.close()
        except Exception:
            record('comfy_connection', False, 'Connection failed; verify local address, TLS/authentication or trusted SSH host key privately')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true', help='Read ComfyUI stats, node/model names and queue')
    args = parser.parse_args()
    result = check(args.online)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(any(item['status'] == 'error' for item in result))


if __name__ == '__main__':
    raise SystemExit(main())
