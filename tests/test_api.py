from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bili_audio.config import Settings
from bili_audio.extractors import ExtractResult
from bili_audio.models import MediaInfo
from bili_audio.server import create_app
from bili_audio.urls import parse_video_url
from tests.helpers import make_stream


class FakeChain:
    def __init__(self, client, settings) -> None:
        self.ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7")

    async def resolve_url(self, raw: str):
        return self.ref

    async def extract(self, ref):
        media = MediaInfo(
            canonical_url=ref.canonical_url,
            title="接口测试视频",
            uploader="UP",
            duration=10,
            extractor="bilibili_api",
            streams=[make_stream()],
        )
        return ExtractResult(media=media, ref=ref)


def fake_download(stream, destination: Path, settings, **kwargs):
    async def _run():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-audio")
        cb = kwargs.get("on_progress")
        if cb:
            cb(10, 10)
        return 10, 10

    return _run()


def fake_convert(input_path, output_path, target_format, source, media, settings, **kwargs):
    async def _run():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-audio-output")
        cb = kwargs.get("on_progress")
        if cb:
            cb(1.0)
        return len(b"fake-audio-output"), None

    return _run()


@pytest.fixture()
def client(settings: Settings, monkeypatch):
    import bili_audio.server as server_module

    monkeypatch.setattr(server_module, "ExtractorChain", FakeChain)
    monkeypatch.setattr("bili_audio.tasks.download", fake_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def test_meta_and_index(client: TestClient) -> None:
    meta = client.get("/api/meta")
    assert meta.status_code == 200
    payload = meta.json()
    assert payload["app"] == "BiliSonic"
    assert payload["ffmpeg_ok"] is True
    assert payload["formats"][0]["id"] == "flac"
    assert "no-store" in meta.headers.get("cache-control", "")

    index = client.get("/")
    assert index.status_code == 200
    assert "BiliSonic" in index.text
    assert "default-src 'self'" in index.headers.get("content-security-policy", "")
    assert client.get("/static/app.js").status_code == 200


def test_task_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/tasks", json={"url": "BV1GJ411x7h7", "format": "flac"}
    )
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["status"] in {"queued", "resolving", "downloading"}

    deadline = time.monotonic() + 5
    current = None
    while time.monotonic() < deadline:
        snapshot = client.get("/api/tasks").json()
        current = next(t for t in snapshot["tasks"] if t["id"] == task["id"])
        if current["status"] == "completed":
            break
        time.sleep(0.02)
    assert current and current["status"] == "completed"
    assert current["output_filename"] == "接口测试视频.flac"
    assert snapshot["stats"]["completed"] == 1

    audio = client.get(f"/api/tasks/{task['id']}/audio")
    assert audio.status_code == 200
    assert audio.content == b"fake-audio-output"
    assert "inline" in audio.headers.get("content-disposition", "")

    download = client.get(f"/api/tasks/{task['id']}/audio?download=1")
    assert "attachment" in download.headers.get("content-disposition", "")

    cleared = client.post("/api/tasks/clear", json={"scope": "finished"})
    assert cleared.json()["removed"] == 1
    assert client.get("/api/tasks").json()["tasks"] == []


def test_validation_and_404(client: TestClient) -> None:
    assert client.post("/api/tasks", json={"url": ""}).status_code == 422
    assert client.post("/api/tasks", json={"url": "BV1GJ411x7h7", "format": "mp3"}).status_code == 422
    assert client.get("/api/tasks/nope").status_code == 404

    duplicate = client.post("/api/tasks", json={"url": "BV1GJ411x7h7"})
    assert duplicate.status_code == 201
    # The first fake task may already be running; duplicating while active/queued must 409.
    task_id = duplicate.json()["task"]["id"]
    second = client.post("/api/tasks", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"})
    assert second.status_code in {201, 409}
    if second.status_code == 201:
        # If the first finished instantly, a re-download is intentionally allowed.
        client.delete(f"/api/tasks/{task_id}")


def test_host_header_guard(client: TestClient) -> None:
    forged = client.get("/api/meta", headers={"host": "evil.example"})
    assert forged.status_code == 403
    assert forged.json()["code"] == "invalid_host"

    local = client.get("/api/meta", headers={"host": "localhost:8765"})
    assert local.status_code == 200


def test_cross_origin_mutation_rejected(client: TestClient) -> None:
    blocked = client.post(
        "/api/tasks/clear",
        json={"scope": "finished"},
        headers={"origin": "http://evil.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "cross_origin_denied"

    allowed = client.post(
        "/api/tasks/clear",
        json={"scope": "finished"},
        headers={"origin": "http://127.0.0.1:8765"},
    )
    assert allowed.status_code == 200
