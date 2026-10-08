# -*- coding: utf-8 -*-
"""Start API server (run from project root in conda env `hat`)."""
import uvicorn

API_PORT = 18765

if __name__ == "__main__":
    print(f"启动 API 服务: http://127.0.0.1:{API_PORT}")
    uvicorn.run("api.main:app", host="0.0.0.0", port=API_PORT, reload=False)
