from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("MODEL_PORT", "8101"))
    uvicorn.run(
        "services.model_service.app:app",
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()

