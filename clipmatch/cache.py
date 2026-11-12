"""Content-addressed completion records; partial files never count as finished work."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, default=str).encode()).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def complete(output: Path, manifest: Path, key: str, stage: str, versions: dict) -> None:
    atomic_json(manifest, dict(version=1, status='complete', stage=stage, key=key,
                              artifact=output.name, size=output.stat().st_size,
                              sha256=file_hash(output), tools=versions))


def cache_valid(output: Path, manifest: Path, key: str) -> bool:
    try:
        record = json.loads(manifest.read_text(encoding='utf-8'))
        return (record.get('version') == 1 and record.get('status') == 'complete'
                and record.get('key') == key and record.get('artifact') == output.name
                and output.stat().st_size > 0 and record.get('size') == output.stat().st_size
                and record.get('sha256') == file_hash(output))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
