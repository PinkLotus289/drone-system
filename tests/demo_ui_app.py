"""Loopback-only browser test fixture. No MQTT, real routes or app startup.

Run: uvicorn demo_ui_app:app --app-dir tests --host 127.0.0.1 --port 8073
This fixture supplies a test identity; never deploy it as the application.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from web_ui.flight_demo import router
from web_ui.real import real_page, STATIC_DIR

app = FastAPI()


@app.middleware("http")
async def test_identity(request, call_next):
    request.state.auth = {"sid": "isolated-browser-test", "op": "test"}
    return await call_next(request)


app.include_router(router)
app.add_api_route("/real", real_page, methods=["GET"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
