"""Vitalis Health Agent 入口。

用法：
    python -m vitalis.main
或：
    uvicorn vitalis.api.app:app --reload
"""

import logging
import os

import uvicorn

from vitalis.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("vitalis.main")


def main() -> None:
    # 启动每日同步调度（生产环境建议独立 worker 常驻）
    if os.getenv("VITALIS_NO_SCHEDULER", "0") != "1":
        try:
            from vitalis.scheduler import start_scheduler

            start_scheduler()
        except Exception as exc:  # 调度器故障不阻塞 API
            log.warning("scheduler start skipped: %s", exc)

    uvicorn.run("vitalis.api.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
