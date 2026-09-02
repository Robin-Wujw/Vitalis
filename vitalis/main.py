"""Vitalis Health Agent 入口。

用法：
    python -m vitalis.main
或：
    uvicorn vitalis.api.app:app --reload
"""

import logging

import uvicorn

from vitalis.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("vitalis.main")


def main() -> None:
    # FastAPI lifespan owns scheduler startup/shutdown for every ASGI launch path.
    uvicorn.run("vitalis.api.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
