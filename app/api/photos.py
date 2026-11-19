"""Photo uploads: price tags, shelf labels and barcodes.

Images are re-encoded through Pillow, which drops EXIF (including GPS) and any
embedded metadata. Files live under PHOTOS_DIR, one folder per year/month, and are
served back by an unguessable token.

Every upload is written twice: a 1280 px copy for the detail view and a 320 px
thumbnail for list views. The list view is what a phone renders while scrolling a
price list on mobile data, so serving camera originals there is the expensive case.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import Photo
from ..schemas import PhotoOut
from ..security import current_device, write_limiter

router = APIRouter(prefix="/v1", tags=["photos"])
log = logging.getLogger(__name__)

MAX_DIMENSION = 1280
THUMB_DIMENSION = 320
JPEG_QUALITY = 82
THUMB_SUFFIX = "_thumb"


def photos_root() -> Path:
    root = Path(settings.photos_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def photo_url(token: str) -> str:
    return f"{settings.public_base_url}/v1/photos/{token}"


def thumb_url(token: str) -> str:
    return f"{settings.public_base_url}/v1/photos/{token}/thumb"


def thumb_filename(filename: str) -> str:
    """Thumbnail path for a stored file: `2026/11/<token>.jpg` -> `.../token_thumb.jpg`.

    Derived from the full name rather than stored, so rows uploaded before thumbnails
    existed need no filename backfill.
    """
    path = PurePosixPath(filename)
    return str(path.with_name(f"{path.stem}{THUMB_SUFFIX}{path.suffix}"))


def photo_out(photo: Photo) -> PhotoOut:
    return PhotoOut(
        id=photo.id,
        token=photo.token,
        url=photo_url(photo.token),
        thumb_url=thumb_url(photo.token),
        content_type=photo.content_type,
        bytes=photo.bytes,
        width=photo.width,
        height=photo.height,
        created_at=photo.created_at,
    )


def decode_image(raw: bytes) -> Image.Image:
    """Decode an upload and flatten it to RGB.

    Re-encoding to RGB/JPEG is what strips EXIF/XMP/GPS, so nothing here may hand the
    original bytes back to the client.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unreadable image: {exc}") from exc
    return image.convert("RGB")


def write_jpeg(image: Image.Image, path: Path, dimension: int) -> None:
    """Encode `image` as JPEG scaled down to fit `dimension`; never upscales."""
    scaled = image.copy()
    if max(scaled.size) > dimension:
        scaled.thumbnail((dimension, dimension))
    path.parent.mkdir(parents=True, exist_ok=True)
    scaled.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)


def store_image(raw: bytes, relative: str) -> tuple[int, int]:
    """Write the full-size copy and its thumbnail, returning the stored dimensions.

    Files are written before the row exists: a crash midway leaves an orphan file that
    the sync script mirrors, which is cheaper to clean up than a row whose URL is broken.
    """
    image = decode_image(raw)
    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    root = photos_root()
    write_jpeg(image, root / relative, MAX_DIMENSION)
    write_jpeg(image, root / thumb_filename(relative), THUMB_DIMENSION)
    return image.width, image.height


def backfill_thumbnail(full: Path, thumb: Path) -> bool:
    """Generate a missing thumbnail for a row uploaded before thumbnails existed.

    Best effort: a read-only or full cache must not turn a list-view image into a 500,
    so callers fall back to the full-size file when this returns False.
    """
    if thumb.is_file():
        return True
    if not full.is_file():
        return False
    tmp = thumb.with_name(f".{thumb.name}.{secrets.token_hex(4)}.tmp")
    try:
        with Image.open(full) as image:
            write_jpeg(image.convert("RGB"), tmp, THUMB_DIMENSION)
        # Atomic swap so a concurrent request never reads a half-written thumbnail.
        os.replace(tmp, thumb)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.warning("thumbnail backfill failed for %s: %s", full, exc)
        return False
    return True


def stored_photo(session: Session, token: str) -> Photo:
    """Look up a token, separating 'never existed' (404) from 'pruned' (410)."""
    photo = session.scalar(select(Photo).where(Photo.token == token))
    if photo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown photo")
    if photo.pruned_at is not None:
        # The bytes are on the backup disk now; saying "gone" is kinder than a 404 that
        # makes a client believe it built the URL wrong.
        raise HTTPException(status.HTTP_410_GONE, "photo pruned to backup storage")
    return photo


def serve(path: Path, media_type: str = "image/jpeg") -> FileResponse:
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/photos", response_model=PhotoOut, status_code=status.HTTP_201_CREATED)
def upload_photo(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> PhotoOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"photo:{device.id}")

    raw = file.file.read()
    if not raw:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty upload")
    if len(raw) > settings.photos_max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image too large")

    digest = hashlib.sha256(raw).hexdigest()
    existing = session.scalar(
        select(Photo).where(
            Photo.sha256 == digest,
            Photo.uploaded_by == device.id,
            # A pruned row's file is on the backup disk; handing its token back would
            # give the client a URL that answers 410.
            Photo.pruned_at.is_(None),
        )
    )
    if existing is not None:
        backfill_thumbnail(
            photos_root() / existing.filename,
            photos_root() / thumb_filename(existing.filename),
        )
        session.commit()
        return photo_out(existing)

    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(18)
    relative = f"{now:%Y/%m}/{token}.jpg"
    width, height = store_image(raw, relative)

    photo = Photo(
        token=token,
        filename=relative,
        content_type="image/jpeg",
        bytes=(photos_root() / relative).stat().st_size,
        sha256=digest,
        width=width,
        height=height,
        uploaded_by=device.id,
    )
    session.add(photo)
    session.commit()
    return photo_out(photo)


@router.get("/photos/{token}", response_class=FileResponse)
def get_photo(token: str, session: Session = Depends(get_session)) -> FileResponse:
    photo = stored_photo(session, token)
    path = photos_root() / photo.filename
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "photo no longer stored")
    return serve(path, photo.content_type)


@router.get("/photos/{token}/thumb", response_class=FileResponse)
def get_photo_thumb(token: str, session: Session = Depends(get_session)) -> FileResponse:
    photo = stored_photo(session, token)
    full = photos_root() / photo.filename
    thumb = photos_root() / thumb_filename(photo.filename)
    # Legacy rows get a thumbnail on first read; if that fails, the full image is still
    # better than an empty tile in the list.
    if not backfill_thumbnail(full, thumb):
        if not full.is_file():
            raise HTTPException(status.HTTP_410_GONE, "photo no longer stored")
        return serve(full, photo.content_type)
    return serve(thumb)
