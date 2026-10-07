"""Fail-closed source and Git-history audit; findings never include matched values."""
import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 5 * 1024 * 1024
RULES = {
    'credential_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'personal_email': re.compile(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'personal_home_path': re.compile(r'(?:/Users/|/home/)[A-Za-z0-9_.-]+|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+'),
    'url_credentials': re.compile(r'https?://[^\s/@:"\']+:[^\s/@"\']+@'),
    'embedded_media': re.compile(r'data:(?:image|audio|video)/[\w.+-]+;base64,[A-Za-z0-9+/=]{100,}'),
    'credential_literal': re.compile(r'''(?i)(?:api[_-]?key|password|secret|access[_-]?token|auth[_-]?token)\s*["']?\s*[:=]\s*["'][A-Za-z0-9_./+=-]{16,}["']'''),
    'network_address': re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'),
}
PRIVATE_PARTS = {'.private', '.venv', 'venv', '.git', '.audit', '.ssh', '.aws', '__pycache__', '.pytest_cache', 'out', 'output', 'node_modules', 'release', 'dist'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.db', '.sqlite', '.log', '.mp4', '.mov', '.webm', '.mp3', '.png', '.jpg', '.jpeg', '.webp', '.wav', '.zip', '.safetensors'}


@dataclass(frozen=True)
class Finding:
    file: str
    rule: str
    line: int = 0


def private_path(name):
    path = PurePosixPath(name)
    return (path.is_absolute() or '..' in path.parts or '\\' in name
            or any(part in PRIVATE_PARTS for part in path.parts)
            or (path.name.startswith('.env') and path.name != '.env.example')
            or path.suffix.lower() in PRIVATE_SUFFIXES or '.local.' in path.name
            or path.name.startswith(('ui_', 'object_info', 'objinfo_')))


def local_secrets(root):
    values = set()
    for path in (root / '.env', root / 'm0/.env'):
        if not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            key, separator, value = line.strip().partition('=')
            value = value.strip().strip('"\'')
            if separator and not key.startswith('#') and any(word in key.upper() for word in ('KEY', 'TOKEN', 'PASS', 'SECRET', 'HOST', 'USER')) and len(value) >= 8:
                if '127.0.0.1' not in value and 'localhost' not in value:
                    values.add(value)
    private = root / 'm0/.private/admin-password'
    if private.is_file():
        value = private.read_text().strip()
        if len(value) >= 8:
            values.add(value)
    return values


def allowed_match(rule, value):
    if rule == 'personal_email':
        return value.endswith(('@example.com', '@example.org', '@example.test', '@users.noreply.github.com'))
    if rule == 'network_address':
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return True
        return address.is_loopback or address.is_unspecified or any(address in ipaddress.ip_network(network) for network in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
    return False


def scan_bytes(name, payload, secrets=()):
    if len(payload) > MAX_BYTES:
        return [Finding(name, 'oversized_file_requires_review')]
    try:
        content = payload.decode('utf-8')
    except UnicodeDecodeError:
        return [Finding(name, 'binary_file_requires_review')]
    findings = []
    for rule, pattern in RULES.items():
        for match in pattern.finditer(content):
            if not allowed_match(rule, match.group()):
                findings.append(Finding(name, rule, content.count('\n', 0, match.start()) + 1))
    for value in secrets:
        position = content.find(value)
        if position >= 0:
            findings.append(Finding(name, 'matches_local_private_configuration', content.count('\n', 0, position) + 1))
    return list(dict.fromkeys(findings))


def source_payloads(root):
    names = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError('Manifest must contain unique relative filenames')
    payloads, findings = {}, []
    secrets = local_secrets(root)
    for name in names:
        if private_path(name):
            findings.append(Finding(name, 'private_or_unsafe_path'))
            continue
        path = root / name
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent) or not path.resolve().is_relative_to(root.resolve()):
            findings.append(Finding(name, 'symlink_forbidden'))
            continue
        if not path.is_file():
            findings.append(Finding(name, 'missing_file'))
            continue
        if path.stat().st_size > MAX_BYTES:
            findings.append(Finding(name, 'oversized_file_requires_review'))
            continue
        payload = path.read_bytes()
        findings.extend(scan_bytes(name, payload, secrets))
        payloads[name] = payload
    return payloads, findings


def git_run(root, *args):
    binary = os.environ.get('GIT_EXECUTABLE') or shutil.which('git')
    if not binary:
        raise RuntimeError('Git unavailable')
    result = subprocess.run([binary, '-C', str(root), *args], capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Git audit failed; inspect Git installation privately')
    return result.stdout


def scan_git(root, history=False):
    findings = []
    secrets = local_secrets(root)
    tracked = git_run(root, 'ls-files', '-z').decode('utf-8').split('\0')
    for name in filter(None, tracked):
        if private_path(name):
            findings.append(Finding(name, 'private_file_tracked'))
        path = root / name
        if path.is_symlink():
            findings.append(Finding(name, 'tracked_symlink'))
        elif path.is_file():
            findings.extend(scan_bytes(name, path.read_bytes(), secrets))
    if history:
        shallow = git_run(root, 'rev-parse', '--is-shallow-repository').strip()
        if shallow == b'true':
            raise RuntimeError('Full history required; shallow checkout cannot pass a history audit')
        objects = git_run(root, 'rev-list', '--objects', '--all').decode('utf-8').splitlines()
        for entry in objects:
            identifier, _, name = entry.partition(' ')
            kind = git_run(root, 'cat-file', '-t', identifier).strip()
            if kind not in {b'blob', b'commit', b'tag'}:
                continue
            label = f'history:{identifier[:12]}'
            if name and private_path(name):
                findings.append(Finding(label, 'private_file_in_history'))
            size = int(git_run(root, 'cat-file', '-s', identifier))
            if size > MAX_BYTES:
                findings.append(Finding(label, 'oversized_history_object'))
                continue
            findings.extend(scan_bytes(label, git_run(root, 'cat-file', '-p', identifier), secrets))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--git', action='store_true', help='Also inspect every currently tracked file')
    parser.add_argument('--history', action='store_true', help='Also inspect all reachable Git objects and commit identities')
    args = parser.parse_args()
    try:
        payloads, findings = source_payloads(args.root)
        if args.git or args.history:
            findings.extend(scan_git(args.root, args.history))
        report = {'source_files': len(payloads), 'history_checked': args.history,
                  'findings': [asdict(item) for item in dict.fromkeys(findings)], 'passed': not findings}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return int(bool(findings))
    except Exception:
        print(json.dumps({'passed': False, 'error': 'Audit could not complete. No file content or credentials are printed.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
