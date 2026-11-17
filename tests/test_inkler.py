import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, "/usr/share/inkscape/extensions")
import inkex
from lxml import etree
from inkler_core import apply_edits, fragment, inspect, revision, SVG, SOD, XML
from inkler_ipc import locked, private_dir, read_json, write_json
from inkler_client import request, pair, connect

ROOT = Path(__file__).resolve().parents[1]
INX_SCHEMA = "/usr/share/inkscape/extensions/inkex/tester/inkscape.extension.rng"
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
                    '<path xmlns="http://evil.invalid"/>',
                    '<path style="fill:url(#a);filter:url(file:///etc/passwd"/>',
                    '<path fill="url (file:///etc/passwd)"/>',
                    '<path id="p"/>stray text<path id="q"/>', '<g><path/>stray</g>', '<path>text</path>',
                    '<text xml:space="bogus">x</text>', '<text xml:base="file:///">x</text>']:
            with self.subTest(svg=svg), self.assertRaises((ValueError, etree.XMLSyntaxError)):
                fragment(svg)

    def test_duplicate_ids_and_root_edits(self):
        root = drawing()
        for op in [{"op": "add", "svg": '<rect id="square"/>'}, {"op": "remove", "id": "canvas"}]:
            with self.subTest(op=op), self.assertRaises(ValueError):
                apply_edits(root, [op], revision(root))

    def test_text_and_xml_space_allowed_where_valid(self):
        nodes = fragment('<text id="t" xml:space="preserve">Big <tspan>dog</tspan> here</text>'
                         '<title>Name</title>\n<path id="after-whitespace"/>')
        self.assertEqual(len(nodes), 3)
        self.assertEqual("".join(nodes[0].itertext()), "Big dog here")
        root = drawing()
        out = apply_edits(root, [{"op": "set", "id": "label", "attrs": {"xml:space": "preserve"}}], revision(root))
        self.assertEqual(out.xpath('//*[@id="label"]')[0].get(f"{{{XML}}}space"), "preserve")

    def test_set_null_removes_references(self):
        root = inkex.load_svg(f'<svg xmlns="{SVG}" xmlns:xlink="http://www.w3.org/1999/xlink" id="r">'
                              '<linearGradient id="g" xlink:href="#h"/><linearGradient id="h"/></svg>').getroot()
        out = apply_edits(root, [{"op": "set", "id": "g", "attrs": {"xlink:href": None}}], revision(root))
        self.assertIsNone(out.xpath('//*[@id="g"]')[0].get("{http://www.w3.org/1999/xlink}href"))

    def test_inspect_omits_long_values_unless_requested(self):
        long_d = "M0 0 " + "L1 1 " * 2000
        root = inkex.load_svg(f'<svg xmlns="{SVG}" id="r"><path id="trace" d="{long_d}" fill="red"/></svg>').getroot()
        row = next(x for x in inspect(root)["objects"] if x["id"] == "trace")
        self.assertNotIn("d", row["attrs"])
        self.assertEqual(row["omitted_attrs"], {"d": len(long_d)})
        self.assertEqual(row["attrs"]["fill"], "red")
        row = inspect(root, ids=["trace"])["objects"][0]
        self.assertEqual(row["attrs"]["d"], long_d)
        self.assertNotIn("omitted_attrs", row)

    def test_attribute_types(self):
        root = drawing()
        with self.assertRaises(ValueError):
            apply_edits(root, [{"op": "set", "id": "square", "attrs": {"fill": 1}}], revision(root))

    @unittest.skipUnless(Path(INX_SCHEMA).is_file(),
                         "This Inkscape package does not ship the INX schema (e.g. Ubuntu 24.04's 1.2.2)")
    def test_inx_schema(self):
        schema = etree.RelaxNG(etree.parse(INX_SCHEMA))
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

    def test_lock_waits_for_other_holder(self):
        path = self.state / "lock"
        released = threading.Event()
        def hold():
            with locked(path):
                released.wait(0.3)
        holder = threading.Thread(target=hold)
        holder.start()
        try:
            time.sleep(0.05)
            with self.assertRaisesRegex(ValueError, "in progress"):
                with locked(path):
                    pass
            with locked(path, wait=5):
                pass
        finally:
            released.set()
            holder.join()

    def test_pending_request_not_overwritten(self):
        write_json(self.state / "request.json", {"request_id": "old"})
        class Fake:
            owner = ":1.2"
            def status(self): return {"document": "doc"}
            def activate(self, timeout=None): raise AssertionError("Must not deliver")
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

    def test_connect_without_code_explains_next_step(self):
        class Fake:
            owner = ":1.100"
            def status(self): return {"document": "doc-1"}
        with self.assertRaisesRegex(ValueError, "Connect Agent"):
            connect("INKLER-ABC", Fake())
        pair(Fake())
        with self.assertRaisesRegex(ValueError, "match"):
            connect("INKLER-ÄBC", Fake())

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
                self.assertEqual(proc.stdout, b"", "Observation must not return a drawing for Inkscape to re-import")
            if operation == "snapshot":
                self.assertTrue((job / "snapshot.svg").is_file())

    def test_client_to_real_extension_roundtrip(self):
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self, timeout=None):
                proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py")],
                                      input=FIXTURE.encode(), capture_output=True, timeout=10)
                if proc.returncode:
                    raise RuntimeError(proc.stderr.decode())
        info = request("inspect", desktop=FakeDesktop())
        result = request("edit", document=info["document"], revision=info["revision"],
                         operations=[{"op": "remove", "id": "square"}], desktop=FakeDesktop())
        self.assertNotEqual(info["revision"], result["revision"])

    def test_large_drawing_snapshot_and_inspection(self):
        # Drawings with traced paths or embedded images exceed the 8 MiB message limit.
        long_d = "M0 0 " + "L123.456 78.9 " * 700000
        fixture = f'<svg xmlns="{SVG}" viewBox="0 0 10 10" id="r"><path id="trace" d="{long_d}"/></svg>'
        self.assertGreater(len(fixture), 9 * 1024 * 1024)
        request_id = "c" * 32
        job = private_dir(self.state / ("job-" + request_id))
        write_json(self.state / "request.json", {"request_id": request_id, "operation": "snapshot"})
        proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py")],
                              input=fixture.encode(), capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        result = read_json(job / "result.json")
        self.assertTrue(result["ok"], result)
        trace = next(x for x in result["data"]["objects"] if x["id"] == "trace")
        self.assertEqual(trace["omitted_attrs"], {"d": len(long_d)})
        self.assertGreater((job / "snapshot.svg").stat().st_size, 9 * 1024 * 1024)

    def test_activation_error_keeps_unknown_outcome(self):
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self, timeout=None): raise RuntimeError("Timeout was reached")
        with self.assertRaisesRegex(TimeoutError, "outcome is unknown.*Timeout was reached"):
            request("inspect", desktop=FakeDesktop(), timeout=0.1)
        self.assertTrue((self.state / "request.json").exists())

    def test_activation_error_still_collects_result(self):
        # Inkscape can run the extension even though the D-Bus reply timed out.
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self, timeout=None):
                proc = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler_effect.py")],
                                      input=FIXTURE.encode(), capture_output=True, timeout=10)
                assert proc.returncode == 0, proc.stderr
                raise RuntimeError("Timeout was reached")
        self.assertIn("revision", request("inspect", desktop=FakeDesktop()))

    def test_install_upgrade_removes_retired_files(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            run = lambda command: subprocess.run(["/usr/bin/python3", str(ROOT / "inkler"), command],
                                                 env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run("install").returncode, 0)
            target = Path(home) / ".config/inkscape/extensions/inkler"
            retired = target / "retired.py"
            retired.write_text("old\n")
            manifest = json.loads((target / "manifest.json").read_text())
            manifest["retired.py"] = hashlib.sha256(b"old\n").hexdigest()
            (target / "manifest.json").write_text(json.dumps(manifest))
            upgrade = run("install")
            self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
            self.assertFalse(retired.exists())
            self.assertNotIn("retired.py", json.loads((target / "manifest.json").read_text()))
            uninstall = run("uninstall")
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse(target.exists())

    def test_timeout_keeps_pending_request(self):
        class FakeDesktop:
            owner = ":1.100"
            def status(self): return {"document": "test-document"}
            def activate(self, timeout=None): pass
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
    def test_observe_uses_one_snapshot_with_matching_image(self):
        from inkler_mcp import tool_call
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp) / "preview.png"
            png.write_bytes(b"test-image")
            png.chmod(0o600)
            data = {"document": "doc", "revision": "live", "image": str(png), "selection": ["cat"]}
            with patch("inkler_client.preview", return_value=data) as preview_mock, patch("inkler_client.request") as request_mock:
                result = tool_call("inkler_observe", {"ids": ["cat"]})
            preview_mock.assert_called_once_with(width=1000, area="page", ids=["cat"])
            request_mock.assert_not_called()
            self.assertEqual(json.loads(result["content"][0]["text"])["revision"], "live")
            self.assertEqual(result["content"][1]["data"], "dGVzdC1pbWFnZQ==")

    def test_edit_returns_live_revision_and_never_repeats_on_preview_failure(self):
        from inkler_mcp import tool_call
        args = {"document": "doc", "revision": "before", "operations": [{"op": "remove", "id": "cat"}]}
        applied = {"document": "doc", "revision": "pre-import", "operations": 1, "note": "Applied"}
        for failure in (None, RuntimeError("renderer failed"), "missing-image", "wrong-document"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                png = Path(temp) / "preview.png"
                if failure != "missing-image":
                    png.write_bytes(b"png")
                    png.chmod(0o600)
                observed = {"document": "other" if failure == "wrong-document" else "doc",
                            "revision": "live", "image": str(png)}
                with patch("inkler_client.request", return_value=applied) as request_mock, patch(
                        "inkler_client.preview", return_value=observed,
                        side_effect=failure if isinstance(failure, Exception) else None):
                    result = tool_call("inkler_edit", args)
                request_mock.assert_called_once_with("edit", **args)
                self.assertFalse(result["isError"])
                data = json.loads(result["content"][0]["text"])
                self.assertTrue(data["edit_applied"])
                if failure is None:
                    self.assertEqual(data["revision"], "live")
                    self.assertEqual(result["content"][1]["type"], "image")
                else:
                    self.assertIn("observation_error", data)
                    self.assertNotIn("revision", data)

    def test_edit_validates_preview_options_before_mutating(self):
        from inkler_mcp import tool_call
        for extra in ({"width": 0}, {"observe": "false"}, {"ids": "cat"}, {"area": "bad"}):
            with patch("inkler_client.request") as request_mock:
                with self.assertRaises(ValueError):
                    tool_call("inkler_edit", {"document": "doc", "revision": "r", "operations": [], **extra})
                request_mock.assert_not_called()

    def test_edit_without_observation_has_no_preimport_revision(self):
        from inkler_mcp import tool_call
        with patch("inkler_client.request", return_value={"document": "doc", "revision": "pre-import", "operations": 1, "note": "Applied"}), patch("inkler_client.preview") as preview_mock:
            result = tool_call("inkler_edit", {"document": "doc", "revision": "r", "operations": [], "observe": False})
        preview_mock.assert_not_called()
        data = json.loads(result["content"][0]["text"])
        self.assertTrue(data["edit_applied"])
        self.assertNotIn("revision", data)

    def test_connect_tool_calls_local_pairing(self):
        from inkler_mcp import tool_call
        with patch("inkler_client.connect", return_value={"connected": True}) as connect_mock:
            result = tool_call("inkler_connect", {"code": "INKLER-TEST"})
        connect_mock.assert_called_once_with(code="INKLER-TEST")
        self.assertFalse(result["isError"])
        self.assertTrue(json.loads(result["content"][0]["text"])["connected"])

    def test_panel_can_outlive_launcher_without_resource_warning(self):
        import warnings
        from inkler_pair import launch_panel
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "panel.py"
            script.write_text("import time; time.sleep(0.3)\n")
            with warnings.catch_warnings(record=True) as emitted:
                warnings.simplefilter("always", ResourceWarning)
                pid = launch_panel(script)
                try:
                    self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))
                    self.assertFalse([w for w in emitted if issubclass(w.category, ResourceWarning)])
                finally:
                    os.waitpid(pid, 0)

    def test_protocol_errors(self):
        messages = [b"{not json\n", b'{"jsonrpc": "2.0", "id": 7, "method": "' + b"x" * (8 * 1024 * 1024) + b'"}\n',
                    b'{"jsonrpc": "2.0", "id": 8, "method": "ping"}\n']
        process = subprocess.run(["/usr/bin/python3", str(ROOT / "inkler"), "mcp"], input=b"".join(messages),
                                 capture_output=True, timeout=30)
        responses = [json.loads(l) for l in process.stdout.splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in responses], [-32700, -32600, None])
        self.assertEqual(responses[2], {"jsonrpc": "2.0", "id": 8, "result": {}})

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
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        self.assertTrue(responses[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
