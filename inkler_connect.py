#!/usr/bin/python3
"""Local pairing panel. No server, no sockets, no remote access."""
from pathlib import Path
import shlex
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
from inkler_client import pair


def instructions(code, root=None):
    root = Path(root or Path(__file__).resolve().parent)
    return (f"Use Inkler to collaborate with me in my existing Inkscape window. "
            f"First read the skill at {str(root / 'SKILL.md')!r}. "
            f"Connect using the inkler_connect MCP tool with code {code}, if that tool is available. "
            f"Otherwise run {shlex.quote(str(root / 'inkler'))} connect {code}. "
            "Inspect my selection and preview the canvas before making the changes I request. "
            "This is a local-only connection; do not launch another Inkscape window or save my file.")


def main():
    window = Gtk.Window(title="Inkler — Connect Agent")
    window.set_default_size(440, -1)
    window.set_border_width(16)
    window.connect("destroy", Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    window.add(box)
    title = Gtk.Label(label="Connect your agent")
    title.set_xalign(0)
    box.pack_start(title, False, False, 0)
    intro = Gtk.Label(label="Copy the instructions and paste them into your agent.")
    intro.set_xalign(0)
    intro.set_line_wrap(True)
    box.pack_start(intro, False, False, 0)
    try:
        session = pair()
        prompt = instructions(session["code"])
        code = Gtk.Label(label=session["code"])
        code.set_selectable(True)
        code.set_xalign(0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(code, True, True, 0)
        box.pack_start(row, False, False, 0)
        text = Gtk.TextView()
        text.set_editable(False)
        text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text(prompt)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(110)
        scroll.add(text)
        details = Gtk.Expander(label="Instructions")
        details.add(scroll)
        box.pack_start(details, True, True, 0)
        button = Gtk.Button()
        button.set_image(Gtk.Image.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON))
        button.set_tooltip_text("Copy instructions")
        button.get_accessible().set_name("Copy instructions")
        feedback = Gtk.Label(label="Local only · Your file stays unsaved")
        feedback.set_xalign(0)
        def copy(_button):
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(prompt, -1)
            clipboard.store()
            feedback.set_text("Copied — paste into your agent")
        button.connect("clicked", copy)
        row.pack_start(button, False, False, 0)
        note = feedback
    except Exception as exc:
        note = Gtk.Label(label="Could not connect: " + str(exc))
        note.set_selectable(True)
    note.set_line_wrap(True)
    note.set_xalign(0)
    box.pack_start(note, False, False, 0)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
