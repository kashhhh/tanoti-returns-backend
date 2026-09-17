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

    _enforce_storage_quota()

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


def _enforce_storage_quota():
    """If the photos directory is at/over the configured budget, delete
    the oldest returns' photos until there's room. Prefers trimming
    already-decided requests first -- only falls back to pending ones
    (which the owner hasn't reviewed yet) if that's not enough, since
    those photos may still matter for a fraud judgment call."""
    from app.models import ReturnRequest, RequestStatus
    from app.extensions import db

    budget_mb = current_app.config["MAX_PHOTO_STORAGE_MB"]
    photos_dir = _photos_dir()

    if _dir_size_mb(photos_dir) < budget_mb:
        return

    for exclude_pending in (True, False):
        query = ReturnRequest.query.filter(ReturnRequest.photo_urls.isnot(None))
        if exclude_pending:
            query = query.filter(ReturnRequest.status != RequestStatus.PENDING)
        oldest_first = query.order_by(ReturnRequest.created_at.asc()).all()

        for r in oldest_first:
            if not r.photo_urls:
                continue
            delete_photos_for_return(r.return_number)
            r.photo_urls = []
            db.session.commit()
            if _dir_size_mb(photos_dir) < budget_mb:
                return


def cleanup_old_photos():
    """Time-based cleanup, independent of the quota-based one above --
    run daily via cron. Deletes photos older than PHOTO_RETENTION_DAYS
    regardless of current storage usage."""
    from app.models import ReturnRequest
    from app.extensions import db

    cutoff = datetime.utcnow() - timedelta(days=current_app.config["PHOTO_RETENTION_DAYS"])
    old_requests = ReturnRequest.query.filter(
        ReturnRequest.created_at < cutoff,
        ReturnRequest.photo_urls.isnot(None),
    ).all()
    for r in old_requests:
        if r.photo_urls:
            delete_photos_for_return(r.return_number)
            r.photo_urls = []
    db.session.commit()