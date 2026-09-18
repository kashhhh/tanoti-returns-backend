"""
Local disk storage for return photos, on the app's own VPS -- no
third-party storage service. Photos are compressed on upload (resized +
re-encoded as JPEG) to keep size down on a small disk, and a quota-based
rolling cleanup deletes the oldest returns' photos automatically if the
configured storage budget is exceeded, so this can never silently fill
the VPS disk.
"""
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image
from flask import current_app

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_DIMENSION = 1600   # longest side in px after resize -- no upscaling
JPEG_QUALITY = 82      # good visual quality, much smaller than source


def _photos_dir() -> Path:
    d = Path(current_app.config["PHOTOS_STORAGE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compress_and_save(file_storage, dest_path: Path) -> Path:
    img = Image.open(file_storage)
    img = img.convert("RGB")  # normalizes mode (e.g. drops PNG alpha); fine for photos

    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    dest_path = dest_path.with_suffix(".jpg")
    img.save(dest_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return dest_path


def upload_return_photo(file_storage, return_number: str) -> str:
    """file_storage: a werkzeug FileStorage from request.files.
    Returns a relative URL path, served by the /uploads/<path> route."""
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")


    return_dir = _photos_dir() / "returns" / return_number
    return_dir.mkdir(parents=True, exist_ok=True)
    dest_path = return_dir / f"{uuid.uuid4().hex}.jpg"
    saved_path = _compress_and_save(file_storage, dest_path)

    relative = saved_path.relative_to(_photos_dir())
    return f"/uploads/{relative.as_posix()}"


def delete_photos_for_return(return_number: str):
    return_dir = _photos_dir() / "returns" / return_number
    if not return_dir.exists():
        return
    for f in return_dir.iterdir():
        f.unlink(missing_ok=True)
    return_dir.rmdir()


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def _all_photo_requests():
    from app.models import ReturnRequest, ExchangeRequest
    return ReturnRequest.query.all() + ExchangeRequest.query.all()


def _number(obj):
    return getattr(obj, "return_number", None) or obj.exchange_number


def _enforce_storage_quota():
    from app.models import RequestStatus
    from app.extensions import db
    budget = current_app.config["MAX_PHOTO_STORAGE_MB"]
    # Preserve unreviewed evidence. If completed requests cannot free enough
    # room, reject the new upload instead of deleting pending photos.
    decided = [r for r in _all_photo_requests() if r.status in (RequestStatus.COMPLETED, RequestStatus.REJECTED)]
    for obj in sorted(decided, key=lambda r: r.created_at):
        if _dir_size_mb(_photos_dir()) <= budget:
            break
        if obj.photo_urls:
            delete_photos_for_return(_number(obj))
            obj.photo_urls = []
    db.session.flush()
    if _dir_size_mb(_photos_dir()) > budget:
        raise ValueError("Photo storage is full. Please contact the store or try again later.")


def required_photos(files):
    photos = []
    for side in ("front", "back"):
        selected = files.getlist(f"photo_{side}")
        if len(selected) != 1 or not selected[0].filename:
            raise ValueError(f"Please upload exactly one {side} photo")
        photo = selected[0]
        if photo.filename.rsplit(".", 1)[-1].lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Photos must be JPG, PNG or WebP")
        photo.stream.seek(0, 2)
        size = photo.stream.tell()
        photo.stream.seek(0)
        if size > 10 * 1024 * 1024:
            raise ValueError("Each photo must be 10 MB or smaller")
        try:
            with Image.open(photo.stream) as img:
                if img.width * img.height > 25_000_000:
                    raise ValueError("Photo resolution is too large; maximum 25 megapixels")
                img.verify()
        except Exception as exc:
            raise ValueError("Please upload a valid JPG, PNG or WebP photo (maximum 25 megapixels)") from exc
        finally:
            photo.stream.seek(0)
        photos.append(photo)
    return photos


def save_request_photos(photos, number):
    try:
        urls = [upload_return_photo(photo, number) for photo in photos]
        _enforce_storage_quota()
        return urls
    except Exception as exc:
        delete_photos_for_return(number)
        raise ValueError(str(exc) if isinstance(exc, ValueError) else "Could not save photos. Please try again.") from exc


def cleanup_old_photos():
    from app.extensions import db
    cutoff = datetime.utcnow() - timedelta(days=current_app.config["PHOTO_RETENTION_DAYS"])
    for obj in _all_photo_requests():
        if obj.created_at < cutoff and obj.photo_urls:
            delete_photos_for_return(_number(obj))
            obj.photo_urls = []
    db.session.commit()
