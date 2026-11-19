"""Photo pipeline: upload downscaling, thumbnails, and how pruned rows answer.

No database is involved: the handlers only use the session to look a row up, so a
one-method stub keeps these runnable with plain `python -m pytest`.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.requests import Request

from app.api import photos
from app.models import Device, Photo


class StubSession:
    """Minimal stand-in for Session: every lookup returns the configured photo."""

    def __init__(self, photo: Photo | None = None) -> None:
        self.photo = photo

    def scalar(self, _statement: object) -> Photo | None:
        return self.photo


class UploadSession:
    """Session stub good enough for one upload: a device, and no duplicate photo.

    `add` fills in what a flush would give the row, so `photo_out` can serialise it.
    """

    def __init__(self, device: Device) -> None:
        self.device = device
        self.added: list[Photo] = []
        self.commits = 0

    def scalar(self, statement: object) -> Device | None:
        entity = statement.column_descriptions[0]["entity"]  # type: ignore[attr-defined]
        return self.device if entity is Device else None

    def add(self, photo: Photo) -> None:
        photo.id = 1
        photo.created_at = datetime.now(timezone.utc)
        self.added.append(photo)

    def commit(self) -> None:
        self.commits += 1


class CountingFile(io.BytesIO):
    """File object that records how many bytes the handler actually consumed."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.consumed += len(chunk)
        return chunk


def counting_upload(payload: bytes) -> tuple[UploadFile, CountingFile]:
    reader = CountingFile(payload)
    return UploadFile(file=reader, filename="tag.jpg"), reader


def upload_request(content_length: int | None = None) -> Request:
    """Request carrying only the headers the upload handler looks at."""
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return Request({"type": "http", "headers": headers})


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


@pytest.fixture()
def quiet_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real limiter is process-wide; one test must not spend another's budget.
    monkeypatch.setattr(photos.write_limiter, "check", lambda _key: None)


def with_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setattr(photos, "settings", replace(photos.settings, photos_max_bytes=limit))


def upload(
    request: Request,
    file: UploadFile,
    session: UploadSession,
) -> photos.PhotoOut:
    return photos.upload_photo(request, file, authorization="Bearer tok", session=session)


def test_oversized_upload_stops_reading_one_chunk_past_the_limit(
    photo_dir: Path,
    quiet_limiter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 4096
    with_limit(monkeypatch, limit)
    # never decoded: the 413 fires before Pillow gets a look at it
    file, reader = counting_upload(b"\xff" * (limit * 4))
    session = UploadSession(Device(id=7))

    with pytest.raises(HTTPException) as excinfo:
        upload(upload_request(), file, session)

    assert excinfo.value.status_code == 413
    assert reader.consumed <= limit + photos.UPLOAD_CHUNK_BYTES
    assert session.added == []
    assert list(photo_dir.rglob("*.jpg")) == []


def test_upload_rejects_an_oversized_content_length_without_reading(
    quiet_limiter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 4096
    with_limit(monkeypatch, limit)
    # the declared size is what a hostile client would send; the body stays tiny so the
    # test cannot itself read gigabytes
    file, reader = counting_upload(jpeg_bytes(64, 64))

    with pytest.raises(HTTPException) as excinfo:
        upload(upload_request(content_length=limit * 1024), file, UploadSession(Device(id=8)))

    assert excinfo.value.status_code == 413
    assert reader.consumed == 0


def test_a_content_length_at_the_limit_still_reads(
    photo_dir: Path,
    quiet_limiter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = jpeg_bytes(64, 64)
    with_limit(monkeypatch, len(payload))
    file, reader = counting_upload(payload)

    # multipart framing makes the body a little larger than the image it carries
    out = upload(
        upload_request(content_length=len(payload) + 512), file, UploadSession(Device(id=9))
    )

    assert out.bytes > 0
    assert reader.consumed == len(payload)


def test_upload_writes_the_full_copy_and_thumbnail(
    photo_dir: Path,
    quiet_limiter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = jpeg_bytes(3000, 2000)
    with_limit(monkeypatch, 2 * 1024 * 1024)
    file, reader = counting_upload(payload)
    session = UploadSession(Device(id=10))

    out = upload(upload_request(), file, session)

    photo = session.added[0]
    full_path = photo_dir / photo.filename
    thumb_path = photo_dir / photos.thumb_filename(photo.filename)
    assert max(Image.open(full_path).size) == photos.MAX_DIMENSION
    assert max(Image.open(thumb_path).size) == photos.THUMB_DIMENSION
    # the digest is taken while streaming and has to match the bytes that were stored
    assert photo.sha256 == hashlib.sha256(payload).hexdigest()
    assert photo.bytes == full_path.stat().st_size
    assert out.bytes == photo.bytes
    assert out.thumb_url.endswith(f"/v1/photos/{photo.token}/thumb")
    assert reader.consumed == len(payload)
    assert session.commits == 1


def test_empty_upload_is_rejected(
    quiet_limiter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_limit(monkeypatch, 4096)
    file, _reader = counting_upload(b"")

    with pytest.raises(HTTPException) as excinfo:
        upload(upload_request(content_length=0), file, UploadSession(Device(id=11)))

    assert excinfo.value.status_code == 422


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
