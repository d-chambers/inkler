#!/usr/bin/python3
"""Explicit desktop test. Open examples/demo.svg as the only document first."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inkler_client import request, preview


def main():
    before = request("inspect")
    rows = {row["id"]: row for row in before["objects"]}
    # Refuse other artwork and refuse replay after the cat has already been added.
    expected = {"demo", "background", "heading", "square", "dog", "dog-body", "dog-head", "caption"}
    if (Path(before["docname"]).name != "demo.svg" or not expected <= rows.keys()
            or rows["caption"].get("text") != "Turn the square into a cat. Point an arrow at the dog."
            or rows["square"]["attrs"].get("fill") != "#18282e" or "cat" in rows):
        sys.exit("Refusing to edit: open the untouched examples/demo.svg fixture as your only document")
    operations = json.loads((ROOT / "examples/cat-and-arrow.json").read_text())
    result = request("edit", document=before["document"], revision=before["revision"], operations=operations)
    after = request("inspect", ids=["square", "cat", "cat-body", "dog", "dog-arrow"])
    ids = {row["id"] for row in after["objects"]}
    if not {"cat", "cat-body", "dog", "dog-arrow"} <= ids or "square" in ids:
        sys.exit("Live edit verification failed. Inspect the canvas before retrying")
    image = preview(width=960)
    print(json.dumps({"live_edit_verified": True, "preview": image["image"],
                      "next": "Press Ctrl+Z once in Inkscape: the square should return and the cat/arrow disappear. Do not save the demo."}, indent=2))


if __name__ == "__main__":
    main()
