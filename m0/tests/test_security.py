import hashlib
import json
from pathlib import Path
import secrets
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from package_release import build
from security_check import scan_bytes, source_payloads


def test_scanner_does_not_echo_secret_values():
    sensitive = 'gh' + 'p_' + secrets.token_hex(20)
    findings = scan_bytes('source.py', sensitive.encode())
    assert findings and findings[0].rule == 'credential_token'
    assert sensitive not in repr(findings)


@pytest.mark.parametrize('email,is_public_service', [
    ('noreply@github.com', True),
    ('support@github.com', True),
    ('person@' + 'github.com', False),
])
def test_scanner_allows_only_exact_github_service_addresses(email, is_public_service):
    findings = scan_bytes('commit.txt', email.encode())
    assert bool(findings) == (not is_public_service)
    assert all(item.rule == 'personal_email' for item in findings)


def test_release_blocks_secrets_private_files_and_symlinks(tmp_path):
    (tmp_path / 'release-manifest.json').write_text(json.dumps(['main.py', '.env', 'link.py']))
    sensitive = secrets.token_urlsafe(32)
    (tmp_path / '.env').write_text('API_KEY=' + sensitive)
    (tmp_path / 'main.py').write_text('value = ' + repr(sensitive))
    (tmp_path / 'link.py').symlink_to(tmp_path / 'main.py')
    _, findings = source_payloads(tmp_path)
    assert {'matches_local_private_configuration', 'private_or_unsafe_path', 'symlink_forbidden'} <= {item.rule for item in findings}
    destination = tmp_path / 'release.zip'
    with pytest.raises(ValueError):
        build(tmp_path, destination)
    assert not destination.exists()


def test_release_ignores_unlisted_runtime_and_has_verified_inventory(tmp_path):
    (tmp_path / 'release-manifest.json').write_text(json.dumps(['main.py']))
    (tmp_path / 'main.py').write_text('print("hello")\n')
    (tmp_path / '.env').write_text('API_KEY=' + secrets.token_urlsafe(32))
    (tmp_path / 'private-video.mp4').write_bytes(b'private')
    destination = tmp_path / 'release.zip'
    assert build(tmp_path, destination) == 1
    with zipfile.ZipFile(destination) as archive:
        assert len(archive.namelist()) == 2
        inventory = json.loads(archive.read('one-sentence-product-video/SHA256SUMS.json'))
        assert inventory['main.py'] == hashlib.sha256(archive.read('one-sentence-product-video/main.py')).hexdigest()


@pytest.mark.parametrize('text,rule', [
    ('person@' + 'private.test', 'personal_email'),
    ('/'.join(['', 'Users', 'private-person', 'file']), 'personal_home_path'),
    ('https://' + 'account:password' + '@example.test', 'url_credentials'),
    ('data:image/png;base64,' + 'a' * 120, 'embedded_media'),
])
def test_privacy_rules_cover_nonkey_leaks(text, rule):
    assert rule in {item.rule for item in scan_bytes('sample.txt', text.encode())}
