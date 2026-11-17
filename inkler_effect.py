#!/usr/bin/python3
"""Inkscape-side structured bridge. Invoked only as an EffectExtension."""
import sys
from pathlib import Path

# System Inkscape libraries; never install or fetch dependencies.
sys.path.insert(0, "/usr/share/inkscape/extensions")
import inkex
from lxml import etree
from inkler_core import apply_edits, inspect, revision
from inkler_ipc import atomic_write, locked, private_dir, read_json, state_dir, write_json


class InklerEffect(inkex.EffectExtension):
    def effect(self):
        state = private_dir(state_dir())
        # The client holds client.lock; a separate lock makes duplicate/manual
        # extension invocations harmless without deadlocking the client.
        with locked(state / "effect.lock"):
            request_path = state / "request.json"
            if not request_path.exists():
                return
            request = read_json(request_path)
            request_id = request.get("request_id", "")
            import re
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                raise inkex.AbortExtension("Invalid Inkler request ID")
            job = state / ("job-" + request_id)
            if not job.is_dir() or job.is_symlink():
                raise inkex.AbortExtension("Missing private Inkler job directory")
            # Claim once; no late action can consume the same operation twice.
            request_path.rename(state / "active.json")
            try:
                operation = request.get("operation")
                if operation == "edit":
                    changed = apply_edits(self.svg, request.get("operations"), request.get("revision"))
                    self.document._setroot(changed)
                    self.svg = changed
                    data = {"revision": revision(changed), "operations": len(request["operations"]),
                            "note": "Applied through Inkscape's undoable extension; save remains under user control."}
                elif operation in {"inspect", "snapshot"}:
                    data = inspect(self.svg, self.options.ids, request.get("ids"))
                    if operation == "snapshot":
                        import copy
                        snapshot = copy.deepcopy(self.svg)
                        # Keep existing relative image links usable in the temporary copy.
                        original_dir = self.svg_path()
                        if original_dir:
                            snapshot.set("{http://www.w3.org/XML/1998/namespace}base",
                                         Path(original_dir).resolve().as_uri() + "/")
                        atomic_write(job / "snapshot.svg", etree.tostring(snapshot))
                        data["snapshot"] = str(job / "snapshot.svg")
                else:
                    raise ValueError("Unknown Inkler operation")
                write_json(job / "result.json", {"request_id": request_id, "ok": True, "data": data})
            except Exception as exc:
                # Do not leave a partially edited tree for inkex to output.
                self.document = self.original_document
                self.svg = self.document.getroot()
                write_json(job / "result.json", {"request_id": request_id, "ok": False,
                                                "error": f"{type(exc).__name__}: {exc}"})
            finally:
                (state / "active.json").unlink(missing_ok=True)


if __name__ == "__main__":
    InklerEffect().run()
