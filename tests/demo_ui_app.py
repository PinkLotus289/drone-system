"""Loopback-only browser test fixture. No MQTT, real routes or app startup.

Run: uvicorn demo_ui_app:app --app-dir tests --host 127.0.0.1 --port 8073
This fixture supplies a test identity; never deploy it as the application.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from tempfile import TemporaryDirectory
from pathlib import Path
from web_ui.flight_demo import router
from web_ui import real
from drone_core.profiles.store import ProfileStore

app = FastAPI()
profiles = TemporaryDirectory(prefix="virtual-port-browser-profiles-")
real.profile_store = ProfileStore(Path(profiles.name))


@app.middleware("http")
async def test_identity(request, call_next):
    request.state.auth = {"sid": "isolated-browser-test", "op": "test"}
    if (
        request.url.path.startswith("/api/real/")
        and not request.url.path.startswith("/api/real/profiles")
        and request.url.path != "/api/real/connections"
    ):
        return JSONResponse({"error": "hardware_disabled_in_browser_test"}, status_code=403)
    return await call_next(request)


app.include_router(router)
app.include_router(real.router)
app.add_api_route("/api/fleet", lambda: {"fleet": []}, methods=["GET"])
app.mount("/static", StaticFiles(directory=real.STATIC_DIR), name="static")
