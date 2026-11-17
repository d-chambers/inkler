#!/usr/bin/python3
"""Open the pairing panel without holding Inkscape's extension transaction open."""
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, "/usr/share/inkscape/extensions")
import inkex


class ConnectAgent(inkex.EffectExtension):
    def effect(self):
        subprocess.Popen(["/usr/bin/python3", str(Path(__file__).with_name("inkler_connect.py"))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    ConnectAgent().run()
