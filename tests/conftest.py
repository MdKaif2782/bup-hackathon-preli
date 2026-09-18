import json
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def load_cases():
    return json.loads(SAMPLES.read_text())["cases"]


@pytest.fixture(scope="session")
def cases():
    return load_cases()
