#!/usr/bin/python3
"""Open the pairing panel without holding Inkscape's extension transaction open."""
from pathlib import Path
import os
import sys
sys.path.insert(0, "/usr/share/inkscape/extensions")
import inkex


def launch_panel(panel_path=None):
    """Spawn a detached panel without an abandoned Popen lifecycle object.

    Inkscape waits for this extension to exit. The panel deliberately outlives
    it, so a Popen object would warn at destruction while its child is alive.
    The OS reparents the child when this short-lived launcher exits.
    """
    panel = str(panel_path or Path(__file__).with_name("inkler_connect.py"))
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0o600),
        (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0o600),
        (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0o600),
    ]
    return os.posix_spawn("/usr/bin/python3", ["/usr/bin/python3", panel],
                          os.environ.copy(), file_actions=actions, setsid=True)


class ConnectAgent(inkex.EffectExtension):
    def effect(self):
        launch_panel()


if __name__ == "__main__":
    ConnectAgent().run()
