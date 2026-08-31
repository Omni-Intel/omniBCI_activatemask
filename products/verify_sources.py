"""Verify frozen product sources against recorded Git blobs, including uncommitted edits."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


# User-authorized packaging repair; runtime and firmware cannot opt out.
PACKAGING_PATHS = {
    ".gitignore", ".github/workflows/build-v16-windows-exe.yml",
    "build_exe.bat", "OmniBCI_V19.spec", "EXE_BUILD_NOTES.txt",
}


def verify_sources(root: Path, manifest: dict) -> list[str]:
    root = root.resolve()
    expected = {}
    errors = []
    for snapshot in manifest["snapshots"]:
        if "commit" in snapshot:
            tree = subprocess.check_output(
                ["git", "ls-tree", "-rz", snapshot["commit"], "--", *snapshot["source_paths"]], cwd=root,
            )
            inventory = {}
            for entry in tree.split(b"\0"):
                if entry:
                    metadata, path = entry.split(b"\t", 1)
                    inventory[snapshot["destination_prefix"] + path.decode("utf-8")] = metadata.split()[2].decode("ascii")
            if inventory != snapshot["files"]:
                errors.append(f"source inventory mismatch: {snapshot['name']}")
        for path, blob in snapshot["files"].items():
            if not (root / path).resolve().is_relative_to(root):
                raise ValueError(f"Source path escapes repository: {path}")
            expected[path] = blob
    for path, blob in manifest.get("packaging_overrides", {}).items():
        if path not in PACKAGING_PATHS or path not in expected:
            raise ValueError(f"Not an authorized packaging override: {path}")
        expected[path] = blob
    present = []
    for path in expected:
        if (root / path).is_file():
            present.append(path)
        else:
            errors.append(f"missing: {path}")
    # Git applies the same text/binary and CRLF rules as a real checkout.
    for offset in range(0, len(present), 40):
        paths = present[offset:offset + 40]
        result = subprocess.run(
            ["git", "hash-object", "--", *paths], cwd=root,
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        blobs = result.stdout.splitlines()
        if len(blobs) != len(paths):
            raise RuntimeError("Git returned an incomplete source checksum list")
        errors.extend(f"modified: {path}" for path, blob in zip(paths, blobs) if blob != expected[path])
    for path, checksum in manifest.get("firmware_canonical_sha256", {}).items():
        if path not in expected:
            errors.append(f"unlisted SHA-256 source: {path}")
            continue
        content = subprocess.check_output(["git", "cat-file", "blob", expected[path]], cwd=root)
        if hashlib.sha256(content).hexdigest() != checksum:
            errors.append(f"SHA-256 mismatch: {path}")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "products/manifest.json").read_text(encoding="utf-8"))
    try:
        errors = verify_sources(root, manifest)
    except subprocess.CalledProcessError:
        print("Source Git objects unavailable or Git failed. Fetch the source branches with full history before verifying.", file=sys.stderr)
        return 1
    for error in errors:
        print(error)
    if not errors:
        count = sum(len(snapshot["files"]) for snapshot in manifest["snapshots"])
        overrides = len(manifest.get("packaging_overrides", {}))
        print(f"PASS: {count} source files match recorded Git blobs ({overrides} authorized packaging updates).")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
