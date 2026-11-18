"""Photo uploads: price tags, shelf labels and barcodes.

Images are re-encoded through Pillow, which drops EXIF (including GPS) and any
embedded metadata. Files live under PHOTOS_DIR, one folder per year/month, and are
served back by an unguessable token.
"""

from __future__ import annotations

import hashlib
import io
import secrets
from datetime import datetime, timezone
from pathlib import Path

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

MAX_DIMENSION = 1600
JPEG_QUALITY = 82


def photos_root() -> Path:
    root = Path(settings.photos_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def photo_url(token: str) -> str:
    return f"{settings.public_base_url}/v1/photos/{token}"


def photo_out(photo: Photo) -> PhotoOut:
    return PhotoOut(
        id=photo.id,
        token=photo.token,
        url=photo_url(photo.token),
        content_type=photo.content_type,
        bytes=photo.bytes,
        width=photo.width,
        height=photo.height,
        created_at=photo.created_at,
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
        select(Photo).where(Photo.sha256 == digest, Photo.uploaded_by == device.id)
    )
    if existing is not None:
        session.commit()
        return photo_out(existing)

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unreadable image: {exc}") from exc

    # Re-encoding to RGB/JPEG is what strips EXIF/XMP/GPS.
    image = image.convert("RGB")
    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))

    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(18)
    relative = f"{now:%Y/%m}/{token}.jpg"
    target = photos_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)

    photo = Photo(
        token=token,
        filename=relative,
        content_type="image/jpeg",
        bytes=target.stat().st_size,
        sha256=digest,
        width=image.width,
        height=image.height,
        uploaded_by=device.id,
    )
    session.add(photo)
    session.commit()
    return photo_out(photo)


@router.get("/photos/{token}", response_class=FileResponse)
def get_photo(token: str, session: Session = Depends(get_session)) -> FileResponse:
    photo = session.scalar(select(Photo).where(Photo.token == token))
    if photo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown photo")
    path = photos_root() / photo.filename
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "photo no longer stored")
    return FileResponse(
        path,
        media_type=photo.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
