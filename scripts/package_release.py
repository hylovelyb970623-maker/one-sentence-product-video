"""Build a source-only ZIP from already-audited in-memory bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from security_check import ROOT, source_payloads


def build(root, destination):
    payloads, findings = source_payloads(root)
    if findings:
        raise ValueError('Security audit failed; run scripts/security_check.py for redacted findings')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Output already exists; select a new filename')
    hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(payloads.items())}
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo('one-sentence-product-video/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.endswith(('.sh', '.command')) else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
        archive.writestr('one-sentence-product-video/SHA256SUMS.json', json.dumps(hashes, indent=2))
    return len(payloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'release/source.zip')
    args = parser.parse_args()
    try:
        count = build(args.root, args.output)
    except (ValueError, OSError):
        print('Release refused. Run the security audit and use a new output filename.')
        return 1
    print(f'Packaged {count} audited source files with SHA-256 inventory; no runtime files included.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
