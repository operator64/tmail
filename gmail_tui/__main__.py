from __future__ import annotations

import os
import sys

from .logging_setup import configure_logging
from .app import GmailTUIApp


def main() -> int:
    configure_logging()

    if not os.environ.get("WT_SESSION"):
        sys.stderr.write(
            "Warning: WT_SESSION not detected. "
            "UI rendering may be degraded outside Windows Terminal.\n"
        )

    app = GmailTUIApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
