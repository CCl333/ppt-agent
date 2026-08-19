from __future__ import annotations

from app.core.config import get_settings
from app.services.orchestrator import BACKGROUND_MAX_BYTES
from tests.helpers import PNG_1X1, make_project


def test_database_file_is_not_served_from_storage(client):
    settings = get_settings()
    leaked = settings.file_storage_root / "ppt_agent.db"
    leaked.write_bytes(b"sqlite-should-not-be-public")

    response = client.get("/storage/ppt_agent.db")

    assert response.status_code == 404


def test_background_upload_rejects_html(client, db_session):
    project = make_project(db_session, stage="init")

    response = client.post(
        f"/api/v1/projects/{project.id}/assets/backgrounds",
        files={"file": ("xss.html", b"<script>alert(1)</script>", "text/html")},
    )

    assert response.status_code == 415
    assert not list(get_settings().background_path.glob(f"{project.id}.*"))


def test_background_upload_rejects_oversized_file(client, db_session):
    project = make_project(db_session, stage="init")
    payload = PNG_1X1 + (b"\x00" * (BACKGROUND_MAX_BYTES - len(PNG_1X1) + 1))

    response = client.post(
        f"/api/v1/projects/{project.id}/assets/backgrounds",
        files={"file": ("huge.png", payload, "image/png")},
    )

    assert response.status_code == 413


def test_background_upload_accepts_png_and_serves_as_attachment(client, db_session):
    project = make_project(db_session, stage="init")

    upload = client.post(
        f"/api/v1/projects/{project.id}/assets/backgrounds",
        files={"file": ("bg.png", PNG_1X1, "image/png")},
    )
    assert upload.status_code == 200

    served = client.get(f"/storage/backgrounds/{project.id}.png")
    assert served.status_code == 200
    assert "attachment" in served.headers.get("content-disposition", "")
    assert served.headers.get("x-content-type-options") == "nosniff"
    assert served.content == PNG_1X1


def test_empty_background_upload_is_rejected(client, db_session):
    project = make_project(db_session, stage="init")
    response = client.post(
        f"/api/v1/projects/{project.id}/assets/backgrounds",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert response.status_code == 415


def test_storage_path_traversal_is_rejected(client):
    response = client.get("/storage/uploads/../ppt_agent.db")
    assert response.status_code in {400, 404}


def test_legacy_html_in_backgrounds_is_not_served_as_html(client):
    html_path = get_settings().background_path / "legacy.html"
    html_path.write_text("<html><body>xss</body></html>", encoding="utf-8")

    response = client.get("/storage/backgrounds/legacy.html")

    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "text/html" not in (response.headers.get("content-type") or "").lower()
