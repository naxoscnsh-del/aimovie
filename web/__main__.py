"""启动：python -m web  （默认 0.0.0.0:8000）"""
import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT") or 8000), log_level="info")
