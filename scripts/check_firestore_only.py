"""Fail the repository check if client or server Cloud Storage use is reintroduced."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "google-cloud-" + "storage",
    "firebase_admin." + "storage",
    "firebase/" + "storage",
    "@firebase/" + "storage",
    "@firebase/" + "firestore",
    "firebase/" + "firestore",
    "storage" + "Bucket",
    "STORAGE_" + "BUCKET",
)
SEARCH_ROOTS = (ROOT / "apps" / "web", ROOT / "services" / "api", ROOT / "firebase", ROOT / "scripts")
SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".toml", ".yaml", ".yml", ".rules", ".example"}


def main() -> int:
    findings: list[str] = []
    for search_root in SEARCH_ROOTS:
        if not search_root.exists():
            continue
        for path in search_root.rglob("*"):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if "node_modules" in path.parts or ".next" in path.parts or "__pycache__" in path.parts:
                continue
            if path.name in {".env", ".env.local"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for value in FORBIDDEN:
                if value in text:
                    findings.append(f"{path.relative_to(ROOT)} contains forbidden storage configuration")
                    break
    root_env_example = ROOT / ".env.example"
    if root_env_example.is_file():
        text = root_env_example.read_text(encoding="utf-8", errors="ignore")
        for value in FORBIDDEN:
            if value in text:
                findings.append(f"{root_env_example.relative_to(ROOT)} contains forbidden storage configuration")
                break
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Firestore-only storage check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
