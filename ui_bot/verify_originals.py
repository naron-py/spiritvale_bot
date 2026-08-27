"""Verify that every pre-UI protected file still matches its baseline hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify(root: str | Path | None = None, manifest: str | Path | None = None):
    package = Path(__file__).resolve().parent
    root = Path(root) if root is not None else package.parent
    manifest = Path(manifest) if manifest is not None else package / "original_manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    changed = []
    for item in data["files"]:
        path = root / item["path"]
        if not path.is_file():
            changed.append((item["path"], "missing"))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            changed.append((item["path"], digest))
    return changed


def main():
    changed = verify()
    if changed:
        for path, detail in changed:
            print(f"CHANGED {path}: {detail}")
        return 1
    print("original terminal project files are byte-for-byte unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
