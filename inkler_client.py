"""Attach to an existing Linux Inkscape session; never launch a GUI."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import secrets
from xml.etree import ElementTree

from inkler_ipc import locked, private_dir, read_json, state_dir, write_json

ACTION = "org.inkler.bridge.noprefs"
BUS_NAME = "org.inkscape.Inkscape"
APP_PATH = "/org/inkscape/Inkscape"


def pair(desktop=None):
    desktop = desktop or Desktop()
    state = private_dir(state_dir())
    with locked(state / "client.lock"):
        if (state / "request.json").exists() or (state / "active.json").exists():
            raise ValueError("An operation is pending; finish it before creating a new connection")
        info = desktop.status()
        code = "INKLER-" + secrets.token_hex(16).upper()
        write_json(state / "connection.json", {"code": code, "document": info["document"]})
        # A new code deliberately disconnects previous agent attachments.
        (state / "agent.json").unlink(missing_ok=True)
        return {"code": code, "document": info["document"], "local_only": True}


def connect(code, desktop=None):
    desktop = desktop or Desktop()
    state = private_dir(state_dir())
    with locked(state / "client.lock"):
        expected = read_json(state / "connection.json")
        if not isinstance(code, str) or not secrets.compare_digest(code.strip(), expected["code"]):
            raise ValueError("Connection code does not match. Use Connect Agent in Inkscape")
        if desktop.status()["document"] != expected["document"]:
            raise ValueError("The paired document closed or Inkscape restarted. Create a new code")
        write_json(state / "agent.json", expected)
        return {"connected": True, "document": expected["document"], "local_only": True}


def check_attachment(state, document):
    # Raw CLI inspection remains useful for diagnostics. Once the user creates a
    # pairing, all drawing operations require that the agent has connected to it.
    if (state / "connection.json").exists():
        if not (state / "agent.json").exists():
            raise ValueError("Connect first: inkler connect CODE (from Inkscape's Connect Agent panel)")
        paired = read_json(state / "connection.json")
        attached = read_json(state / "agent.json")
        if attached != paired or attached["document"] != document:
            raise ValueError("Connection changed. Use Connect Agent and connect with its new code")


class Desktop:
    def __init__(self):
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # No auto-start: we attach only to a running app, and address its unique owner.
        self.owner = self.connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetNameOwner", GLib.Variant("(s)", (BUS_NAME,)), None,
            Gio.DBusCallFlags.NO_AUTO_START, 3000, None).unpack()[0]

    def call(self, path, interface, method, signature=None, args=()):
        params = self.GLib.Variant(signature, args) if signature else None
        return self.connection.call_sync(
            self.owner, path, interface, method, params, None,
            self.Gio.DBusCallFlags.NO_AUTO_START, 5000, None).unpack()

    def children(self, path):
        xml = self.call(path, "org.freedesktop.DBus.Introspectable", "Introspect")[0]
        return [path + "/" + el.get("name") for el in ElementTree.fromstring(xml).findall("node")
                if el.get("name", "").isdigit()]

    def status(self):
        windows = self.children(APP_PATH + "/window")
        documents = self.children(APP_PATH + "/document")
        if len(windows) != 1 or len(documents) != 1:
            raise ValueError("Prototype requires exactly one Inkscape window/document open; close the others first")
        actions = self.call(APP_PATH, "org.gtk.Actions", "List")[0]
        if ACTION not in actions:
            raise ValueError("Inkler extension is unavailable. Run 'inkler install', save your work, and restart Inkscape")
        return {"connected": True, "document": self.owner + documents[0],
                "window": windows[0], "transport": "local session D-Bus", "auto_save": False}

    def activate(self):
        self.call(APP_PATH, "org.gtk.Actions", "Activate", "(sava{sv})", (ACTION, [], {}))


def request(operation, *, document=None, revision=None, operations=None, ids=None,
            desktop=None, timeout=20):
    if operation not in {"inspect", "snapshot", "edit"}:
        raise ValueError("Unknown operation")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 1500 or
                            not all(isinstance(x, str) for x in ids)):
        raise ValueError("ids must be a list of at most 1,500 object IDs")
    desktop = desktop or Desktop()
    state = private_dir(state_dir())
    with locked(state / "client.lock"):
        status = desktop.status()
        check_attachment(state, status["document"])
        if operation == "edit" and (document != status["document"] or not revision):
            raise ValueError("Document changed or missing inspection token. Inspect again before editing")
        for name in ("request.json", "active.json"):
            if (state / name).exists():
                raise ValueError("An Inkler request is still pending. Do not retry edits; check Inkscape. "
                                 "After restarting Inkscape, use 'inkler reset --after-restart'")
        request_id = uuid.uuid4().hex
        job = private_dir(state / ("job-" + request_id))
        spec = {"request_id": request_id, "operation": operation, "revision": revision,
                "operations": operations, "ids": ids, "owner": desktop.owner}
        write_json(state / "request.json", spec)
        # A delivery timeout has an unknown outcome: retain the pending request,
        # preventing a later action from consuming a different edit.
        desktop.activate()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (job / "result.json").exists() and not (state / "active.json").exists():
                result = read_json(job / "result.json")
                if result.get("request_id") != request_id:
                    raise ValueError("Mismatched Inkler response")
                if not result.get("ok"):
                    raise ValueError(result.get("error", "Inkler extension failed"))
                data = result["data"]
                data["document"] = status["document"]
                return data
            time.sleep(0.03)
        raise TimeoutError("Inkler request timed out; outcome is unknown. Check Inkscape before retrying. "
                           "If a dialog is blocking it, dismiss it. Otherwise restart Inkscape and reset Inkler")


def preview(*, width=1200, area="page", ids=None, desktop=None):
    if type(width) is not int or not 64 <= width <= 2048 or area not in {"page", "drawing"}:
        raise ValueError("Preview width must be 64–2048; area must be page or drawing")
    data = request("snapshot", ids=ids, desktop=desktop)
    snapshot = Path(data.pop("snapshot"))
    png = snapshot.parent / "preview.png"
    # This is a headless render process, not another editor window. Never use a shell.
    subprocess.run(["/usr/bin/inkscape", "--app-id-tag=inkler-render", "--batch-process",
                    str(snapshot), "--export-type=png", "--export-filename=" + str(png),
                    "--export-width=" + str(width), "--export-area-" + area],
                   capture_output=True, check=True, timeout=20)
    os.chmod(png, 0o600)
    data["image"] = str(png)
    data["note"] = "Preview of live document content, not the editor chrome; user units are given by viewBox"
    return data


def reset_after_restart():
    state = private_dir(state_dir())
    with locked(state / "client.lock"), locked(state / "effect.lock"):
        owners = []
        for name in ("request.json", "active.json"):
            path = state / name
            if path.exists():
                owners.append(read_json(path).get("owner"))
        if owners:
            # Refuse to clear a request while the original process still owns D-Bus.
            try:
                current = Desktop().owner
            except Exception:
                current = None
            if current is None or current in owners:
                raise ValueError("Cannot verify Inkscape restarted. Open Inkscape again after quitting it")
        for name in ("request.json", "active.json"):
            (state / name).unlink(missing_ok=True)
    return {"reset": True}
