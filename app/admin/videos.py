"""Admin-only video evidence. Files never live under a public upload directory."""
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file, abort
from sqlalchemy import or_
from werkzeug.exceptions import RequestEntityTooLarge

from app.extensions import db
from app.models import Order, OrderItem, OrderVideo, ReturnRequest, ExchangeRequest
from app.security import lock_login
from app.utils.admin_auth import admin_required

videos_bp = Blueprint("videos", __name__)
KINDS = {"packing", "return_opening", "exchange_packing"}
MB = 1024 * 1024


def directory():
    path = Path(current_app.config["VIDEOS_STORAGE_DIR"]).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_path(row):
    extension = "webm" if row.content_type == "video/webm" else "mp4"
    return directory() / f"{row.id}.{extension}"


def serialize(row):
    return dict(id=row.id, kind=row.kind, request_number=row.request_number,
                size_bytes=row.size_bytes, created_by=row.created_by,
                created_at=row.created_at.isoformat() + "Z",
                url=f"/api/admin/videos/{row.id}/content")



def packing_videos_by_order(order_ids):
    grouped = {order_id: [] for order_id in order_ids}
    if grouped:
        rows = OrderVideo.query.filter(
            OrderVideo.order_id.in_(grouped),
            OrderVideo.kind.in_(("packing", "exchange_packing")),
        ).order_by(OrderVideo.created_at.desc(), OrderVideo.id.desc()).all()
        for row in rows:
            grouped[row.order_id].append(serialize(row))
    return grouped


def order_summary(order):
    return dict(id=order.id, order_number=order.order_number,
                shopify_order_id=order.shopify_order_id, customer=order.customer.name,
                email=order.customer.email)


@videos_bp.get("/orders")
@admin_required
def orders():
    q = request.args.get("q", "").strip().lstrip("#")[:64]
    query = Order.query
    if q:
        query = query.filter(or_(Order.order_number == q, Order.order_number == "#" + q,
                                 Order.shopify_order_id == q))
    rows = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(30).all()
    packing = packing_videos_by_order([row.id for row in rows])
    return jsonify(orders=[{**order_summary(row), "packing_video_count": len(packing[row.id])} for row in rows])


@videos_bp.get("/orders/<int:order_id>")
@admin_required
def order_videos(order_id):
    order = db.get_or_404(Order, order_id)
    requests = []
    for model, kind, column in ((ReturnRequest, "return", ReturnRequest.return_number),
                                (ExchangeRequest, "exchange", ExchangeRequest.exchange_number)):
        for obj in model.query.join(OrderItem).filter(OrderItem.order_id == order.id).all():
            requests.append(dict(number=getattr(obj, column.key), type=kind))
    rows = OrderVideo.query.filter_by(order_id=order.id).order_by(OrderVideo.created_at.desc()).all()
    return jsonify(order=order_summary(order), requests=requests, videos=[serialize(row) for row in rows],
                   max_upload_mb=current_app.config["MAX_VIDEO_UPLOAD_MB"])


@videos_bp.post("")
@admin_required
def upload():
    # Increase only this authenticated route's limit; customer photo limits stay intact.
    max_bytes = current_app.config["MAX_VIDEO_UPLOAD_MB"] * MB
    request.max_content_length = max_bytes + MB
    try:
        order_id = int(request.form.get("order_id", ""))
    except ValueError:
        return jsonify(error="Choose an order first."), 400
    order = db.get_or_404(Order, order_id)
    kind = request.form.get("kind")
    number = request.form.get("request_number") or None
    if kind not in KINDS:
        return jsonify(error="Choose a valid recording type."), 400
    if kind == "packing" and number:
        return jsonify(error="Original packing belongs to the order, not a request."), 400
    if kind != "packing":
        models = (ExchangeRequest,) if kind == "exchange_packing" else (ReturnRequest, ExchangeRequest)
        matched = False
        for model in models:
            column = model.return_number if model == ReturnRequest else model.exchange_number
            matched = matched or model.query.join(OrderItem).filter(
                column == number, OrderItem.order_id == order.id).first() is not None
        if not matched:
            return jsonify(error="Select a matching return or exchange request for this order."), 400
    try:
        video_id = uuid.UUID(request.form.get("upload_id", "")).hex
    except (ValueError, AttributeError):
        return jsonify(error="Invalid upload ID. Please select the video again."), 400
    files = request.files.getlist("video")
    if len(files) != 1:
        return jsonify(error="Choose one MP4 or WebM video."), 400
    stream = files[0].stream
    header = stream.read(64)
    stream.seek(0)
    # Check the actual container signature, never trust the filename or MIME header.
    if header.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in header:
        content_type = "video/webm"
    elif len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in (
            b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  "):
        content_type = "video/mp4"
    else:
        return jsonify(error="This file is not a supported MP4 or WebM recording."), 400
    temporary = None
    saved = None
    try:
        # Shared PostgreSQL lock serializes quota checks and retry IDs across workers.
        lock_login("admin-video-storage")
        existing = db.session.get(OrderVideo, video_id)
        digest = hashlib.sha256()
        size = 0
        if existing:
            # A retry after a lost response needs no additional disk space.
            while chunk := stream.read(MB):
                size += len(chunk)
                if size > max_bytes:
                    raise RequestEntityTooLarge()
                digest.update(chunk)
            if (existing.order_id, existing.kind, existing.request_number, existing.sha256) != (order.id, kind, number, digest.hexdigest()):
                return jsonify(error="This upload ID was already used for another recording."), 409
            if not video_path(existing).is_file():
                return jsonify(error="The saved video file is missing. Contact the administrator to restore it."), 503
            return jsonify(video=serialize(existing)), 200
        root = directory()
        used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        free = shutil.disk_usage(root).free
        budget = current_app.config["MAX_VIDEO_STORAGE_MB"] * MB
        reserve = current_app.config["VIDEO_MIN_FREE_MB"] * MB
        with tempfile.NamedTemporaryFile(dir=root, prefix="pending-", delete=False) as target:
            temporary = Path(target.name)
            while True:
                chunk = stream.read(MB)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise RequestEntityTooLarge()
                if used + size > budget or free - size < reserve:
                    return jsonify(error="Video storage is full or disk space is low. Increase storage before uploading."), 507
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size < 64:
            return jsonify(error="Video is empty or incomplete."), 400
        checksum = digest.hexdigest()
        row = OrderVideo(id=video_id, order_id=order.id, kind=kind, request_number=number,
                         content_type=content_type, size_bytes=size, sha256=checksum, created_by=g.admin_email)
        saved = video_path(row)
        os.replace(temporary, saved)
        temporary = None
        db.session.add(row)
        db.session.commit()
        saved = None
        return jsonify(video=serialize(row)), 201
    except RequestEntityTooLarge:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save admin video")
        return jsonify(error="Could not save the video. Keep this page open and retry."), 503
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        if saved:
            saved.unlink(missing_ok=True)


@videos_bp.get("/<video_id>/content")
@admin_required
def content(video_id):
    if not re.fullmatch(r"[a-f0-9]{32}", video_id):
        abort(404)
    row = db.get_or_404(OrderVideo, video_id)
    path = video_path(row)
    if not path.is_file():
        return jsonify(error="Video file is unavailable. Check the video storage mount or backup."), 404
    return send_file(path, mimetype=row.content_type, conditional=True,
                     download_name=f"{row.kind}-{row.id}{path.suffix}",
                     as_attachment=request.args.get("download") == "1", max_age=0)
