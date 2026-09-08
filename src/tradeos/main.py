"""Entrypoint: `python -m tradeos.main` (or scripts/run.sh)."""
from __future__ import annotations

import uvicorn

from tradeos.api.server import create_server
from tradeos.config import get_settings


def main() -> None:
    settings = get_settings()
    server = create_server()
    uvicorn.run(server, host=settings.api_host, port=settings.api_port,
                log_level="info")


if __name__ == "__main__":
    main()
