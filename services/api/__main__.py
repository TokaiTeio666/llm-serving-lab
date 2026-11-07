from __future__ import annotations

import uvicorn

from services.common.config import get_api_settings


def main() -> None:
    settings = get_api_settings()
    uvicorn.run(
        "services.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()

