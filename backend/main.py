from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from app.api import create_app


HOST = os.getenv('AUAV_BACKEND_HOST', '127.0.0.1')
PORT = int(os.getenv('AUAV_BACKEND_PORT', '18080'))
WORK_DIR = Path(os.getenv('APP_WORK_DIR', Path(__file__).resolve().parent / 'work'))

app = create_app(WORK_DIR)


if __name__ == '__main__':
    uvicorn.run(app, host=HOST, port=PORT, reload=False, workers=1)


