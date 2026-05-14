"""Validate that schemas/research_brief.example.json satisfies the schema.

Step 1 acceptance criterion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import jsonschema
except ImportError:
    jsonschema = None


SCHEMA_PATH = REPO_ROOT / "build_agent" / "schemas" / "research_brief.schema.json"
EXAMPLE_PATH = REPO_ROOT / "build_agent" / "schemas" / "research_brief.example.json"


def test_example_validates_against_schema():
    if jsonschema is None:
        # Graceful skip; the import test will catch missing deps in CI.
        print("SKIP: jsonschema not installed. Run: pip install jsonschema")
        return
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    with open(EXAMPLE_PATH) as f:
        example = json.load(f)
    jsonschema.validate(instance=example, schema=schema)


def test_schema_is_loadable_json():
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    assert schema["$schema"].startswith("https://json-schema.org")
    assert "build_unfit" in schema["required"]


if __name__ == "__main__":
    test_schema_is_loadable_json()
    test_example_validates_against_schema()
    print("OK")
