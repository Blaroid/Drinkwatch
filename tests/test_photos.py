"""Photo pipeline: upload downscaling, thumbnails, and how pruned rows answer.

No database is involved: the handlers only use the session to look a row up, so a
one-method stub keeps these runnable with plain `python -m pytest`.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api import photos
from app.models import Photo


class StubSession:
    """Minimal stand-in for Session: every lookup returns the configured photo."""

    def __init__(self, photo: Photo | None = None) -> None:
        self.photo = photo

    def scalar(self, _statement: object) -> Photo | None:
        return self.photo


def jpeg_bytes(width: int, height: int, with_exif: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), (200, 30, 30))
    buffer = io.BytesIO()
    options: dict = {"quality": 95}
    if with_exif:
        exif = Image.Exif()
        exif[0x0112] = 6  # orientation: the tag phone cameras usually leave behind
        options["exif"] = exif
    image.save(buffer, "JPEG", **options)
    return buffer.getvalue()


@pytest.fixture()
def photo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(photos, "photos_root", lambda: tmp_path)
    return tmp_path


def test_store_image_writes_downscaled_copy_and_thumbnail(photo_dir: Path) -> None:
    width, height = photos.store_image(jpeg_bytes(3000, 2000, with_exif=True), "2026/11/tok.jpg")

    full_path = photo_dir / "2026/11/tok.jpg"
    thumb_path = photo_dir / "2026/11/tok_thumb.jpg"
    full = Image.open(full_path)
    thumb = Image.open(thumb_path)

    assert (width, height) == full.size
    assert max(full.size) == photos.MAX_DIMENSION
    assert max(thumb.size) == photos.THUMB_DIMENSION
    assert thumb_path.stat().st_size < full_path.stat().st_size
    # re-encoding dropped the EXIF block
    assert len(full.getexif()) == 0


def test_store_image_never_upscales(photo_dir: Path) -> None:
    photos.store_image(jpeg_bytes(64, 64), "2026/11/small.jpg")

    assert Image.open(photo_dir / "2026/11/small.jpg").size == (64, 64)
    assert Image.open(photo_dir / "2026/11/small_thumb.jpg").size == (64, 64)


def test_thumb_filename_is_derived_from_the_stored_name() -> None:
    assert photos.thumb_filename("2026/11/tok.jpg") == "2026/11/tok_thumb.jpg"


def test_unreadable_upload_is_rejected() -> None:
    with pytest.raises(HTTPException) as excinfo:
        photos.decode_image(b"definitely not a jpeg")

    assert excinfo.value.status_code == 422


def test_thumb_endpoint_backfills_rows_uploaded_before_thumbnails(photo_dir: Path) -> None:
    (photo_dir / "2026/11").mkdir(parents=True)
    Image.new("RGB", (1600, 1200), (10, 120, 10)).save(photo_dir / "2026/11/legacy.jpg", "JPEG")
    photo = Photo(
        token="legacy",
        filename="2026/11/legacy.jpg",
        content_type="image/jpeg",
        pruned_at=None,
    )

    response = photos.get_photo_thumb("legacy", session=StubSession(photo))

    thumb_path = Path(response.path)
    assert thumb_path.name == "legacy_thumb.jpg"
    assert max(Image.open(thumb_path).size) == photos.THUMB_DIMENSION
    # the atomic temp file must not survive a successful backfill
    assert [p.name for p in (photo_dir / "2026/11").glob("*.tmp")] == []


def test_pruned_photo_is_gone_rather_than_missing(photo_dir: Path) -> None:
    pruned = Photo(
        token="tok",
        filename="2026/11/tok.jpg",
        pruned_at=datetime.now(timezone.utc),
    )
    for handler in (photos.get_photo, photos.get_photo_thumb):
        with pytest.raises(HTTPException) as excinfo:
            handler("tok", session=StubSession(pruned))
        assert excinfo.value.status_code == 410

    with pytest.raises(HTTPException) as excinfo:
        photos.get_photo("nope", session=StubSession(None))
    assert excinfo.value.status_code == 404


def test_photo_out_advertises_the_thumbnail_url() -> None:
    photo = Photo(
        id=1,
        token="tok",
        content_type="image/jpeg",
        bytes=1234,
        width=1280,
        height=853,
        created_at=datetime.now(timezone.utc),
    )

    out = photos.photo_out(photo)

    assert out.thumb_url is not None and out.thumb_url.endswith("/v1/photos/tok/thumb")
