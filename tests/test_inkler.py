import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, "/usr/share/inkscape/extensions")
import inkex
from lxml import etree
from inkler_core import apply_edits, fragment, inspect, revision, SVG, SOD
from inkler_ipc import locked, private_dir, read_json, write_json
from inkler_client import request, pair, connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = f'''<svg xmlns="{SVG}" xmlns:sodipodi="{SOD}" width="400" height="250" viewBox="0 0 400 250" id="canvas">
<sodipodi:namedview id="view" zoom="1"/>
<rect id="square" x="40" y="40" width="80" height="80" fill="black"/>
<g id="dog" transform="translate(220,100)"><ellipse id="dog-body" rx="45" ry="25" fill="tan"/></g>
<text id="label" x="10" y="200">Dog</text></svg>'''


def drawing():
    return inkex.load_svg(FIXTURE).getroot()


class CoreTests(unittest.TestCase):
    def test_inspect_geometry(self):
        root = drawing()
        before = etree.tostring(root)
        data = inspect(root, ["square"])
        self.assertEqual(etree.tostring(root), before)
        self.assertEqual(data["revision"], revision(root))
        self.assertEqual(data["selection"], ["square"])
        square = next(x for x in data["objects"] if x["id"] == "square")
        self.assertEqual(square["bbox"], [40, 40, 80, 80])
        dog = next(x for x in data["objects"] if x["id"] == "dog-body")
        for measured, expected in zip(dog["bbox"], [175, 75, 90, 50]):
            self.assertAlmostEqual(measured, expected, places=2)

    def test_revision_ignores_viewport(self):
        root = drawing()
        before = revision(root)
        root[0].set("zoom", "2")
        self.assertEqual(before, revision(root))
        root[1].set("fill", "red")
        self.assertNotEqual(before, revision(root))

    def test_replace_square_and_add_arrow(self):
        root = drawing()
        result = apply_edits(root, [
            {"op": "replace", "id": "square", "svg": '<g id="cat"><path id="cat-head" d="M40 110 L40 40 L65 60 L95 60 L120 40 L120 110 Z" fill="black"/></g>'},
            {"op": "add", "svg": '<defs><marker id="arrow-tip" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z"/></marker></defs><path id="arrow" d="M130 150 L200 110" stroke="red" marker-end="url(#arrow-tip)"/>'},
        ], revision(root))
        self.assertFalse(result.xpath('//*[@id="square"]'))
        self.assertTrue(result.xpath('//*[@id="cat"]'))
        self.assertTrue(result.xpath('//*[@id="arrow"]'))
        self.assertTrue(root.xpath('//*[@id="square"]'), "Input tree must remain unchanged")

    def test_atomic_batch_failure(self):
        root = drawing()
        before = etree.tostring(root)
        with self.assertRaises(ValueError):
            apply_edits(root, [{"op": "remove", "id": "square"}, {"op": "remove", "id": "missing"}], revision(root))
        self.assertEqual(before, etree.tostring(root))

    def test_stale_revision(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_edits(drawing(), [{"op": "remove", "id": "square"}], "old")

    def test_text_set_remove(self):
        root = drawing()
        out = apply_edits(root, [{"op": "text", "id": "label", "text": "A dog"},
                                 {"op": "set", "id": "square", "attrs": {"fill": "blue"}},
                                 {"op": "remove", "id": "dog"}], revision(root))
        self.assertEqual(out.xpath('//*[@id="label"]')[0].text, "A dog")
        self.assertEqual(out.xpath('//*[@id="square"]')[0].get("fill"), "blue")
        self.assertFalse(out.xpath('//*[@id="dog"]'))

    def test_reject_active_svg(self):
        for svg in ['<script/>', '<foreignObject/>', '<image href="file:///etc/passwd"/>',
                    '<path onclick="alert(1)"/>', '<use href="https://evil.invalid/x"/>',
                    '<path fill="url(file:///etc/passwd)"/>', '<style>@import "x";</style>',
                    '<!DOCTYPE x><path/>', '<path style="fill:url(\\68 ttp://evil.invalid)"/>',
                    '<path xmlns="http://evil.invalid"/>']:
            with self.subTest(svg=svg), self.assertRaises((ValueError, etree.XMLSyntaxError)):
                fragment(svg)

    def test_duplicate_ids_and_root_edits(self):
        root = drawing()
        for op in [{"op": "add", "svg": '<rect id="square"/>'}, {"op": "remove", "id": "canvas"}]:
            with self.subTest(op=op), self.assertRaises(ValueError):
                apply_edits(root, [op], revision(root))

    def test_attribute_types(self):
        root = drawing()
        with self.assertRaises(ValueError):
            apply_edits(root, [{"op": "set", "id": "square", "attrs": {"fill": 1}}], revision(root))

    def test_inx_schema(self):
        schema = etree.RelaxNG(etree.parse('/usr/share/inkscape/extensions/inkex/tester/inkscape.extension.rng'))
        for name in ('inkler.inx', 'inkler_connect.inx'):
            self.assertTrue(schema.validate(etree.parse(str(ROOT / name))), str(schema.error_log))


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"INKLER_STATE_DIR": self.tmp.name})
        self.env.start()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_private_files(self):
        write_json(self.state / "a.json", {"hello": "world"})
        self.assertEqual((self.state / "a.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_json(self.state / "a.json"), {"hello": "world"})

    def test_symlink_refusal(self):
        write_json(self.state / "a.json", {})
        (self.state / "b.json").symlink_to(self.state / "a.json")
        with self.assertRaises(OSError):
            read_json(self.state / "b.json")
        with self.assertRaises(ValueError):
            private_dir(self.state / "b.json")

    def test_cross_process_lock(self):
        with locked(self.state / "lock"):
            child = subprocess.run(["/usr/bin/python3", "-c",
                "from inkler_ipc import locked; from pathlib import Path; import sys; "
                "exec('with locked(Path(sys.argv[1])): pass')", str(self.state / "lock")],
                cwd=ROOT, capture_output=True)
            self.assertNotEqual(child.returncode, 0)

    def test_pending_request_not_overwritten(self):
        write_json(self.state / "request.json", {"request_id": "old"})
        class Fake:
            owner = ":1.2"
            def status(self): return {"document": "doc"}
            def activate(self): raise AssertionError("Must not deliver")
        with self.assertRaisesRegex(ValueError, "pending"):
            request("inspect", desktop=Fake())
        self.assertEqual(read_json(self.state / "request.json"), {"request_id": "old"})

    def test_document_identity(self):
        class Fake:
            owner = ":1.2"
            def status(self): return {"document": "new-doc"}
        with self.assertRaisesRegex(ValueError, "Document changed"):
            request("edit", document="old-doc", revision="x", operations=[], desktop=Fake())

    def test_pair_code_attaches_only_current_document(self):
        class Fake:
            owner = ":1.100"
            def status(self): return {"document": "doc-1"}
        session = pair(Fake())
        self.assertTrue(session["code"].startswith("INKLER-"))
        with self.assertRaisesRegex(ValueError, "match"):
            connect("wrong", Fake())
        with self.assertRaisesRegex(ValueError, "Connect first"):
            request("inspect", desktop=Fake())
        attached = connect(session["code"], Fake())
        self.assertEqual(attached["document"], "doc-1")
        class Changed(Fake):
            def status(self): return {"document": "doc-2"}
        with self.assertRaisesRegex(ValueError, "closed"):
            connect(session["code"], Changed())
        with self.assertRaisesRegex(ValueError, "Connection changed"):
            request("inspect", desktop=Changed())
        new = pair(Fake())
        self.assertNotEqual(session["code"], new["code"])
        self.assertFalse((self.state / "agent.json").exists())

    def test_copy_instructions_contain_skill_and_command(self):
        from inkler_connect import instructions
        text = instructions("INKLER-TEST", Path("/tmp/my inkler"))
        self.assertIn("/tmp/my inkler/SKILL.md", text)
        self.assertIn("'/tmp/my inkler/inkler' connect INKLER-TEST", text)

    def test_real_extension_inspect_and_edit(self):
        # Run the actual inkex subprocess, passing a fixture, without a GUI.
        for operation in ("inspect", "edit", "snapshot"):
            request_id = "a" * 32
            job = private_dir(self.state / ("job-" + request_id))
            write_json(self.state / "request.json", {"request_id": request_id, "operation": operation,
                       "revision": revision(drawing()),
                       "operations": [{"op": "set", "id": "square", "attrs": {"fill": "blue"}}]})
            proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py"), "--id=square"],
                                  input=FIXTURE.encode(), capture_output=True, env=os.environ, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            result = read_json(job / "result.json")
            self.assertTrue(result["ok"], result)
            if operation == "edit":
                self.assertEqual(etree.fromstring(proc.stdout).xpath('//*[@id="square"]')[0].get("fill"), "blue")
            else:
                self.assertEqual(result["data"]["selection"], ["square"])
            if operation == "snapshot":
                self.assertTrue((job / "snapshot.svg").is_file())

    def test_client_to_real_extension_roundtrip(self):
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self):
                proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py")],
                                      input=FIXTURE.encode(), capture_output=True, timeout=10)
                if proc.returncode:
                    raise RuntimeError(proc.stderr.decode())
        info = request("inspect", desktop=FakeDesktop())
        result = request("edit", document=info["document"], revision=info["revision"],
                         operations=[{"op": "remove", "id": "square"}], desktop=FakeDesktop())
        self.assertNotEqual(info["revision"], result["revision"])

    def test_timeout_keeps_pending_request(self):
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self): pass
        with self.assertRaises(TimeoutError):
            request("inspect", desktop=FakeDesktop(), timeout=0)
        self.assertTrue((self.state / "request.json").exists())
        with self.assertRaisesRegex(ValueError, "pending"):
            request("inspect", desktop=FakeDesktop(), timeout=0)

    def test_failed_edit_outputs_no_partial_svg(self):
        request_id = "b" * 32
        job = private_dir(self.state / ("job-" + request_id))
        write_json(self.state / "request.json", {"request_id": request_id, "operation": "edit",
                   "revision": revision(drawing()), "operations": [
                       {"op": "remove", "id": "square"}, {"op": "remove", "id": "missing"}]})
        proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py")],
                              input=FIXTURE.encode(), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(read_json(job / "result.json")["ok"])
        self.assertEqual(proc.stdout, b"", "Failed batch must not output a modified drawing")


class MCPTests(unittest.TestCase):
    def test_protocol(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "not-a-tool"}},
        ]
        process = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler"), "mcp"],
                                 input="\n".join(json.dumps(m) for m in messages) + "\n",
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(l) for l in process.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "inkler")
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)
        self.assertTrue(responses[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
