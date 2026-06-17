from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.app import ArtifactDetailResponse
from apps.api.platform_api import ArtifactDownload
from packages.shared_types import EntityNotFoundError, ErrorCode, ErrorCodeContractError


class _FakePlatformAPI:
    def __init__(self) -> None:
        self.artifacts_by_run = {
            "run_alpha": (
                ArtifactDetailResponse(
                    artifact_id="artifact_text",
                    run_id="run_alpha",
                    artifact_type="log",
                    label="stdout.log",
                    uri="memory://stdout",
                    created_at="2026-06-15T00:00:00Z",
                    content="hello world",
                    content_inline=True,
                    content_kind="text",
                ),
            )
        }
        self.artifacts_by_id = {
            "artifact_text": ArtifactDetailResponse(
                artifact_id="artifact_text",
                run_id="run_alpha",
                artifact_type="log",
                label="stdout.log",
                uri="memory://stdout",
                created_at="2026-06-15T00:00:00Z",
                content="hello world",
                content_inline=True,
                content_kind="text",
            ),
            "artifact_bin": ArtifactDetailResponse(
                artifact_id="artifact_bin",
                run_id="run_alpha",
                artifact_type="patch",
                label="binary.dat",
                uri="memory://binary",
                created_at="2026-06-15T00:00:00Z",
                content=None,
                content_inline=False,
                content_kind="binary",
                content_note="Binary artifact is not inlined.",
            ),
        }
        self.missing_run_ids: set[str] = set()

    async def list_artifacts(self, run_id: str) -> tuple[ArtifactDetailResponse, ...]:
        if run_id in self.missing_run_ids:
            raise EntityNotFoundError("run", run_id)
        return self.artifacts_by_run.get(run_id, ())

    async def get_artifact(self, artifact_id: str) -> ArtifactDetailResponse | None:
        return self.artifacts_by_id.get(artifact_id)

    async def get_artifact_download(self, artifact_id: str) -> ArtifactDownload | None:
        if artifact_id == "artifact_text":
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "Artifact artifact_text does not expose a downloadable file.",
            )
        if artifact_id == "artifact_missing":
            return None
        return ArtifactDownload(
            artifact_id=artifact_id,
            path=Path(__file__),
            media_type="text/plain",
            filename="artifact.txt",
        )


def test_list_run_artifacts_returns_200():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/runs/run_alpha/artifacts")

    assert response.status_code == 200
    assert response.json() == [
        {
            "artifact_id": "artifact_text",
            "run_id": "run_alpha",
            "artifact_type": "log",
            "label": "stdout.log",
            "uri": "memory://stdout",
            "size_bytes": None,
            "created_at": "2026-06-15T00:00:00Z",
            "content": "hello world",
            "content_inline": True,
            "content_kind": "text",
            "content_note": None,
            "download_uri": None,
        }
    ]


def test_list_run_artifacts_returns_404_when_run_missing():
    platform_api = _FakePlatformAPI()
    platform_api.missing_run_ids.add("run_missing")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/runs/run_missing/artifacts")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_get_artifact_returns_textual_content_when_inline():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_text")

    assert response.status_code == 200
    assert response.json()["content"] == "hello world"
    assert response.json()["content_inline"] is True
    assert response.json()["content_kind"] == "text"
    assert response.json()["download_uri"] is None


def test_get_artifact_returns_non_inline_metadata_for_binary_artifact():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_bin")

    assert response.status_code == 200
    assert response.json()["content"] is None
    assert response.json()["content_inline"] is False
    assert response.json()["content_kind"] == "binary"
    assert response.json()["content_note"] == "Binary artifact is not inlined."
    assert response.json()["download_uri"] is None


def test_download_artifact_returns_file_response():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_bin/download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment; filename=\"artifact.txt\"" in response.headers["content-disposition"]


def test_download_artifact_returns_400_when_artifact_is_not_downloadable():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_text/download")

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "Artifact artifact_text does not expose a downloadable file.",
        }
    }


def test_get_artifact_returns_404_when_missing():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "artifact not found: artifact_missing"}
    }
