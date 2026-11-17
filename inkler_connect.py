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
            f"Then run {shlex.quote(str(root / 'inkler'))} connect {code}. "
            "Inspect my selection and preview the canvas before making the changes I request. "
            "This is a local-only connection; do not launch another Inkscape window or save my file.")


def main():
    window = Gtk.Window(title="Inkler — Connect Agent")
    window.set_default_size(650, 330)
    window.set_border_width(22)
    window.connect("destroy", Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    window.add(box)
    title = Gtk.Label(label="Connect an agent to this drawing")
    title.set_xalign(0)
    box.pack_start(title, False, False, 0)
    intro = Gtk.Label(label="Copy these instructions and paste them into a local coding agent.\n"
                           "Your drawing stays in this Inkscape window. You control saving and Undo.")
    intro.set_xalign(0)
    intro.set_line_wrap(True)
    box.pack_start(intro, False, False, 0)
    try:
        session = pair()
        prompt = instructions(session["code"])
        code = Gtk.Label(label=session["code"])
        code.set_selectable(True)
        code.set_xalign(0)
        box.pack_start(code, False, False, 0)
        text = Gtk.TextView()
        text.set_editable(False)
        text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text(prompt)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(110)
        scroll.add(text)
        box.pack_start(scroll, True, True, 0)
        button = Gtk.Button(label="Copy instructions")
        def copy(_button):
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(prompt, -1)
            clipboard.store()
            button.set_label("Copied — paste into your agent")
        button.connect("clicked", copy)
        box.pack_start(button, False, False, 0)
        note = Gtk.Label(label="Local desktop only · No listening port · Code expires when this document closes")
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
