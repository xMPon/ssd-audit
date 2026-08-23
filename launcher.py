"""Entry point for the packaged executable.

PyInstaller needs a plain script to start from, and freezing a module run with
``-m`` is unreliable. This exists solely to give it one.
"""

import sys

from ssdaudit.gui import main

if __name__ == "__main__":
    sys.exit(main())
