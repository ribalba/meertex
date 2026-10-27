"""The browser's file list must match the server's (app/static/js/livefiles.js
against app/live/ydoc.py).

Builds documents with pycrdt, including path clashes, conflict names that are
taken already, odd file names and malformed entries, and compares what
ydoc.materialize() and the JavaScript port make of each one. Run it with the
repository's virtualenv, which has pycrdt (see tools/editor/README.md).
"""

import base64
import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path

from pycrdt import Doc, Map, Text

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("ydoc", REPO / "app" / "live" / "ydoc.py")
ydoc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ydoc)

NODE = """
import { readFileSync } from "node:fs";
const [bundle, livefiles] = process.argv.slice(1);
const { Y } = await import(bundle);
const { materialize } = await import(livefiles);
const out = [];
for (const update of JSON.parse(readFileSync(0, "utf8"))) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, Buffer.from(update, "base64"));
  const files = {};
  for (const [path, e] of materialize(doc.getMap("files"), Y)) files[path] = [e.id, e.kind];
  out.push(files);
}
console.log(JSON.stringify(out));
"""

NAMES = ["main.tex", "intro.tex", "refs.bib", ".gitignore", "a.b.c", "Makefile", "Übersicht.tex",
         "fig 1.png", "😀.tex", "x.conflict-00000000.tex", "data.tar.gz", ".hidden.txt"]
FOLDERS = ["", "sections/", "figures/", "Kapitel/", "a/b/", "sections/old/"]
IDS = "0123456789abcdef"


def random_doc(rng: random.Random) -> Doc:
    doc = Doc()
    files = doc.get("files", type=Map)
    ids = set()
    for _ in range(rng.randint(1, 14)):
        file_id = "".join(rng.choice(IDS) for _ in range(32))
        # Share a prefix now and then, so conflict names collide on 8 characters.
        if ids and rng.random() < 0.2:
            file_id = rng.choice(sorted(ids))[:8] + file_id[8:]
        ids.add(file_id)
        path = rng.choice(FOLDERS) + rng.choice(NAMES)
        roll = rng.random()
        if roll < 0.04:
            path = rng.choice(["../up.tex", ".meertex/x", "a//b", "/abs.tex", "", ".git/config"])
        if roll < 0.08 and rng.random() < 0.5:
            files[file_id] = Map({"path": path, "kind": "weird"})
        elif rng.random() < 0.7:
            files[file_id] = Map({"path": path, "kind": "text", "text": Text("x")})
        else:
            files[file_id] = Map({"path": path, "kind": "blob", "sha": "ab" * 32, "size": float(rng.randint(0, 99))})
    if rng.random() < 0.1:
        files["not-a-map"] = "just a string"
    if rng.random() < 0.1:
        files["ffffffff" + "0" * 24] = Map({"kind": "text"})  # no path
    # A file named exactly like another one's conflict copy.
    if rng.random() < 0.3 and ids:
        other = rng.choice(sorted(ids))
        files["0" * 32] = Map({"path": ydoc.conflict_path("intro.tex", other), "kind": "text", "text": Text("")})
        files["1" * 32] = Map({"path": "intro.tex", "kind": "text", "text": Text("")})
        files[other] = Map({"path": "intro.tex", "kind": "text", "text": Text("")})
    return doc


def main() -> None:
    rng = random.Random(20271027)
    docs = [random_doc(rng) for _ in range(400)]
    updates = [base64.b64encode(d.get_update()).decode() for d in docs]
    bundle = (REPO / "app" / "static" / "vendor" / "editor" / "editor.js").as_uri()
    livefiles = (REPO / "app" / "static" / "js" / "livefiles.js").as_uri()
    result = subprocess.run(["node", "--input-type=module", "-e", NODE, bundle, livefiles],
                            input=json.dumps(updates), capture_output=True, text=True, check=True)
    js = json.loads(result.stdout)
    bad = 0
    clashes = 0
    for doc, got in zip(docs, js):
        want = {path: [e.id, e.kind] for path, e in ydoc.materialize(doc).items()}
        clashes += any(".conflict-" in p for p in want)
        if got != want:
            bad += 1
            if bad <= 3:
                print("Python:", json.dumps(want, indent=1, ensure_ascii=False))
                print("JavaScript:", json.dumps(got, indent=1, ensure_ascii=False))
    if bad:
        print(f"FAIL  {bad} of {len(docs)} documents differ")
        sys.exit(1)
    print(f"ok    {len(docs)} documents agree, {clashes} of them with path clashes")


if __name__ == "__main__":
    main()
