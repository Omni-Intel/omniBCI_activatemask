"""OmniBCI V19 packaged entry point."""

from __future__ import annotations

import os

os.environ["OMNIBCI_APP_RELEASE_VERSION"] = "19"

from onmibci_gui.app import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
