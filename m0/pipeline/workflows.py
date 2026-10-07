"""Portable MiniMax H3 API workflows with configurable node roles."""
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROLES = {
    'generator': '3', 'product_image': '18', 'first_frame': '27',
    'person_image': '28', 'reference_video': '13', 'video_components': '16',
    'seed': '21', 'output': '17',
}


def configuration():
    path = os.environ.get('DIRECTOR_WORKFLOW_CONFIG')
    if not path:
        return {}, ROOT
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Workflow configuration requires version 1')
    return data, path.parent


def load(kind, template_path=None):
    data, base = configuration()
    settings = data.get(kind, {})
    if not isinstance(settings, dict):
        raise ValueError('Workflow settings must be an object')
    configured = settings.get('template')
    path = Path(template_path) if template_path else (base / configured if configured else ROOT / 'workflows' / f'{kind}.json')
    workflow = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(workflow, dict) or not workflow or not all(
        isinstance(node, dict) and isinstance(node.get('class_type'), str) and isinstance(node.get('inputs'), dict)
        for node in workflow.values()
    ):
        raise ValueError('Workflow must be ComfyUI API JSON, not a UI export')
    roles = DEFAULT_ROLES | settings.get('nodes', {})
    if any(key not in DEFAULT_ROLES or not isinstance(value, str) for key, value in roles.items()):
        raise ValueError('Workflow node roles require known role names and string node IDs')
    if len(set(roles.values())) != len(roles):
        raise ValueError('Workflow node roles must reference distinct node IDs')
    required = ('generator', 'seed', 'output', 'product_image' if kind == 'ref2va' else 'first_frame')
    if any(roles[key] not in workflow for key in required):
        raise ValueError('Workflow is missing required mapped nodes')
    return workflow, roles


def output_prefix(workflow, prefix, kind='ref2va'):
    data, _ = configuration()
    node = data.get(kind, {}).get('nodes', {}).get('output', DEFAULT_ROLES['output'])
    if node not in workflow or 'filename_prefix' not in workflow[node]['inputs']:
        raise ValueError('Mapped output node must accept filename_prefix')
    workflow[node]['inputs']['filename_prefix'] = prefix
