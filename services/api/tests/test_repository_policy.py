import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.check_firestore_only import main


def test_firestore_only_repository_check() -> None:
    assert main() == 0
