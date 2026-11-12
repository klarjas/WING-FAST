import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from typing import Optional
from urllib.parse import parse_qsl

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TELEGRAM_STORAGE_CHANNEL_ID = os.getenv("TELEGRAM_STORAGE_CHANNEL_ID", "").strip()
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

app = FastAPI(title="WING FAST API", version="2.0.0")
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")


@contextmanager
def db():
    if not SUPABASE_DB_URL:
        raise HTTPException(503, "SUPABASE_DB_URL is not configured on the server")
    conn = psycopg.connect(SUPABASE_DB_URL, row_factory=dict_row, connect_timeout=15)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(503, "Server is not configured: BOT_TOKEN is missing")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = pairs.pop("hash", "")
        auth_date = int(pairs.get("auth_date", "0"))
        if not received_hash or not auth_date:
            raise ValueError()
        if abs(int(time.time()) - auth_date) > INIT_DATA_MAX_AGE:
            raise HTTPException(401, "Telegram session expired. Reopen the Mini App.")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            raise ValueError()
        user = json.loads(pairs["user"])
        if not user.get("id"):
            raise ValueError()
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid Telegram authentication data")


def current_user(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Open WING FAST inside Telegram")
    return verify_telegram_init_data(authorization[4:].strip())


def ensure_user(conn, tg: dict) -> dict:
    telegram_id = int(tg["id"])
    full_name = (str(tg.get("first_name", "")) + " " + str(tg.get("last_name", ""))).strip()[:160]
    username = str(tg.get("username", ""))[:80] or None
    role = "admin" if str(telegram_id) in ADMIN_IDS else "passenger"
    conn.execute("""
        INSERT INTO public.users (telegram_user_id, full_name, username, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (telegram_user_id) DO UPDATE SET
            full_name=EXCLUDED.full_name,
            username=EXCLUDED.username,
            role=CASE WHEN public.users.role='admin' THEN 'admin'
                      WHEN EXCLUDED.role='admin' THEN 'admin'
                      ELSE public.users.role END,
            updated_at=NOW()
    """, (telegram_id, full_name, username, role))
    row = conn.execute("""
        SELECT id, telegram_user_id, full_name, username, phone, role, account_status,
               EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at
        FROM public.users WHERE telegram_user_id=%s
    """, (telegram_id,)).fetchone()
    if row["account_status"] != "active":
        raise HTTPException(403, "Your account is suspended. Contact WING FAST support.")
    return row


def auth_user(authorization):
    tg = current_user(authorization)
    with db() as conn:
        return ensure_user(conn, tg)


def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")


def get_driver(conn, user_uuid):
    return conn.execute("SELECT * FROM public.drivers WHERE user_id=%s", (user_uuid,)).fetchone()


def require_driver(conn, user_uuid):
    driver = get_driver(conn, user_uuid)
    if not driver or driver["status"] != "approved":
        raise HTTPException(403, "Driver account is not approved")
    return driver



ALLOWED_MEDIA_CATEGORIES = {"vehicle", "profile", "document", "other"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_DOCUMENT_TYPES = ALLOWED_IMAGE_TYPES | {"application/pdf"}


def telegram_upload_file(field_name: str, filename: str, content_type: str, content: bytes) -> dict:
    if not BOT_TOKEN or not TELEGRAM_STORAGE_CHANNEL_ID:
        raise HTTPException(503, "Telegram image storage is not configured on the server")
    boundary = "----WINGFAST" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_STORAGE_CHANNEL_ID}\r\n".encode()
    ]
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
         f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
        + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/"
        + ("sendPhoto" if field_name == "photo" else "sendDocument"),
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(502, f"Telegram upload failed: {type(exc).__name__}")
    if not result.get("ok"):
        raise HTTPException(502, "Telegram rejected the uploaded file")
    return result["result"]


@app.post("/api/media")
async def upload_media(
    file: UploadFile = File(...),
    category: str = Form(default="other"),
    authorization: Optional[str] = Header(default=None),
):
    user = auth_user(authorization)
    if category not in ALLOWED_MEDIA_CATEGORIES:
        raise HTTPException(422, "Invalid media category")
    content_type = (file.content_type or "").lower()
    allowed_types = ALLOWED_DOCUMENT_TYPES if category == "document" else ALLOWED_IMAGE_TYPES
    if content_type not in allowed_types:
        raise HTTPException(415, "Only JPG, PNG, WEBP images and PDF documents are accepted")
    if category != "document" and content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(415, "This category only accepts images")
    max_size = 20 * 1024 * 1024 if category == "document" else 10 * 1024 * 1024
    content = await file.read(max_size + 1)
    if not content or len(content) > max_size:
        raise HTTPException(413, "File is empty or exceeds the upload size limit")
    filename = os.path.basename(file.filename or "upload")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "upload"
    field_name = "photo" if category != "document" else "document"
    telegram_message = telegram_upload_file(field_name, filename, content_type, content)
    stored = telegram_message.get("photo", [])[-1] if field_name == "photo" else telegram_message.get("document")
    if not stored or not stored.get("file_id"):
        raise HTTPException(502, "Telegram did not return a file ID")
    with db() as conn:
        row = conn.execute("""
            INSERT INTO public.media_files
                (telegram_file_id, telegram_file_unique_id, telegram_message_id,
                 category, file_name, mime_type, file_size, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, category, file_name, mime_type, file_size, created_at
        """, (stored["file_id"], stored.get("file_unique_id"), telegram_message.get("message_id"),
              category, filename, content_type, len(content), user["id"])).fetchone()
    return {
        "ok": True,
        "media": {
            "id": str(row["id"]),
            "category": row["category"],
            "file_name": row["file_name"],
            "mime_type": row["mime_type"],
            "file_size": row["file_size"],
            "url": f"/api/media/{row['id']}",
        },
    }


@app.get("/api/media/{media_id}")
def get_media(media_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        media = conn.execute("""
            SELECT id, telegram_file_id, category, file_name, mime_type, uploaded_by
            FROM public.media_files WHERE id=%s
        """, (media_id,)).fetchone()
    if not media:
        raise HTTPException(404, "Media not found")
    if media["category"] == "document" and str(media["uploaded_by"]) != str(user["id"]):
        require_admin(user)
    if not BOT_TOKEN:
        raise HTTPException(503, "Telegram bot is not configured")
    try:
        info_request = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            data=json.dumps({"file_id": media["telegram_file_id"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(info_request, timeout=20) as response:
            info = json.loads(response.read().decode("utf-8"))
        if not info.get("ok"):
            raise RuntimeError("Telegram getFile failed")
        file_path = info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        with urllib.request.urlopen(file_url, timeout=30) as response:
            content = response.read()
    except Exception as exc:
        raise HTTPException(502, f"Could not retrieve media from Telegram: {type(exc).__name__}")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", media["file_name"] or "media")
    return Response(
        content=content,
        media_type=media["mime_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )

def clean(value, maximum, label):
    value = value.strip()
    if not value or len(value) > maximum:
        raise HTTPException(422, f"{label} is required and must be under {maximum} characters")
    return value


class DriverApplication(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    vehicle_type: str = Field(pattern="^(car|suv|tuk_tuk|motorbike|van)$")
    vehicle_model: str = Field(min_length=2, max_length=100)
    plate_number: str = Field(min_length=2, max_length=30)

    @field_validator("phone", "vehicle_model", "plate_number")
    @classmethod
    def trim(cls, value):
        return value.strip()


class BookingCreate(BaseModel):
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lon: float = Field(ge=-180, le=180)
    pickup_text: str = Field(default="", max_length=180)
    destination_text: str = Field(min_length=2, max_length=180)
    destination_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    destination_lon: Optional[float] = Field(default=None, ge=-180, le=180)
    vehicle_type: str = Field(default="all", pattern="^(all|car|suv|tuk_tuk|motorbike|van)$")
    payment_method: str = Field(default="cash", pattern="^(cash|digital)$")
    distance_mode: str = Field(default="road", pattern="^(road|map)$")
    estimated_distance_km: Optional[float] = Field(default=None, ge=0, le=10000)


class LocationUpdate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = Field(default=None, ge=0, le=100000)


class BookingStatusUpdate(BaseModel):
    status: str = Field(pattern="^(arrived|in_progress|completed|cancelled)$")


@app.get("/")
def home():
    return FileResponse(os.path.join(APP_DIR, "index.html"))


@app.get("/api/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "app": "WING FAST", "version": app.version, "database": "connected"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Database connection failed: {type(exc).__name__}")


@app.get("/api/me")
def me(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = get_driver(conn, user["id"])
        vehicle = None
        if driver:
            vehicle = conn.execute("""
                SELECT vehicle_type, model AS vehicle_model, plate_number
                FROM public.vehicles WHERE driver_id=%s
                ORDER BY created_at DESC LIMIT 1
            """, (driver["id"],)).fetchone()
    ui_user = dict(user)
    ui_user["telegram_id"] = str(user["telegram_user_id"])
    ui_user["first_name"] = user["full_name"]
    ui_user["blocked"] = False
    ui_driver = None
    if driver:
        ui_driver = {"id": str(driver["id"]), "status": driver["status"]}
        if vehicle:
            ui_driver.update(dict(vehicle))
    return {"user": ui_user, "driver": ui_driver}


@app.post("/api/driver/apply")
def driver_apply(payload: DriverApplication, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    plate = clean(payload.plate_number, 30, "License plate")
    normalized = re.sub(r"[^A-Z0-9]", "", plate.upper())
    with db() as conn:
        existing = conn.execute("""
            SELECT id FROM public.vehicles
            WHERE normalized_plate=%s AND status IN ('pending','approved','blocked')
        """, (normalized,)).fetchone()
        driver = get_driver(conn, user["id"])
        if existing:
            owner = conn.execute("SELECT driver_id FROM public.vehicles WHERE id=%s", (existing["id"],)).fetchone()
            if not driver or str(owner["driver_id"]) != str(driver["id"]):
                raise HTTPException(409, "This license plate is already registered or blocked")
        conn.execute("UPDATE public.users SET phone=%s, updated_at=NOW() WHERE id=%s",
                     (payload.phone, user["id"]))
        if not driver:
            driver = conn.execute("""
                INSERT INTO public.drivers (user_id, status)
                VALUES (%s, 'pending') RETURNING *
            """, (user["id"],)).fetchone()
        else:
            conn.execute("""
                UPDATE public.drivers SET status='pending', rejection_reason=NULL,
                    online_status=FALSE, updated_at=NOW() WHERE id=%s
            """, (driver["id"],))
        conn.execute("""
            INSERT INTO public.vehicles
                (driver_id, vehicle_type, model, plate_number, normalized_plate, status)
            VALUES (%s,%s,%s,%s,%s,'pending')
        """, (driver["id"], payload.vehicle_type, payload.vehicle_model, plate, normalized))
    return {"ok": True, "status": "pending", "message": "Application submitted for admin review"}


@app.post("/api/driver/location")
def driver_location(payload: LocationUpdate, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = require_driver(conn, user["id"])
        conn.execute("""
            INSERT INTO public.driver_locations
                (driver_id, latitude, longitude, accuracy, is_online, updated_at)
            VALUES (%s,%s,%s,%s,TRUE,NOW())
            ON CONFLICT (driver_id) DO UPDATE SET
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
                accuracy=EXCLUDED.accuracy, is_online=TRUE, updated_at=NOW()
        """, (driver["id"], payload.lat, payload.lon, payload.accuracy))
        conn.execute("UPDATE public.drivers SET online_status=TRUE, updated_at=NOW() WHERE id=%s",
                     (driver["id"],))
    return {"ok": True}



@app.get("/api/drivers/live")
def live_drivers(authorization: Optional[str] = Header(default=None)):
    auth_user(authorization)
    with db() as conn:
        rows = conn.execute("""
            SELECT d.id AS driver_id, dl.latitude, dl.longitude,
                   v.vehicle_type, v.model AS vehicle_model, v.plate_number,
                   EXTRACT(EPOCH FROM dl.updated_at)::BIGINT AS updated_at
            FROM public.driver_locations dl
            JOIN public.drivers d ON d.id=dl.driver_id
            JOIN public.users u ON u.id=d.user_id
            LEFT JOIN LATERAL (
                SELECT vehicle_type, model, plate_number FROM public.vehicles
                WHERE driver_id=d.id AND status='approved'
                ORDER BY created_at DESC LIMIT 1
            ) v ON TRUE
            WHERE d.status='approved' AND u.account_status='active'
              AND dl.is_online=TRUE AND dl.updated_at > NOW() - INTERVAL '2 minutes'
            ORDER BY dl.updated_at DESC LIMIT 500
        """).fetchall()
    return {"items":[dict(row) for row in rows]}


@app.post("/api/bookings")
def create_booking(payload: BookingCreate, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    if (payload.destination_lat is None) != (payload.destination_lon is None):
        raise HTTPException(422, "Destination coordinates must include both latitude and longitude")
    with db() as conn:
        price = conn.execute("""
            SELECT base_fare, price_per_km, minimum_fare
            FROM public.pricing
            WHERE vehicle_type=%s AND is_active=TRUE
        """, (payload.vehicle_type,)).fetchone()
        estimated_fare = None
        if price and payload.estimated_distance_km is not None:
            estimated_fare = max(
                float(price["minimum_fare"]),
                float(price["base_fare"]) + float(price["price_per_km"]) * payload.estimated_distance_km
            )
        row = conn.execute("""
            INSERT INTO public.bookings (
                passenger_id, pickup_latitude, pickup_longitude, pickup_address,
                destination_latitude, destination_longitude, destination_address,
                vehicle_type, payment_method, distance_mode, estimated_distance_km,
                estimated_fare, status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'requested')
            RETURNING id
        """, (user["id"], payload.pickup_lat, payload.pickup_lon,
              payload.pickup_text.strip(), payload.destination_lat, payload.destination_lon,
              clean(payload.destination_text, 180, "Destination"),
              payload.vehicle_type, payload.payment_method, payload.distance_mode,
              payload.estimated_distance_km, estimated_fare)).fetchone()
    return {"ok": True, "booking_id": str(row["id"]), "status": "searching"}


def booking_rows(conn, where, params):
    return conn.execute(f"""
        SELECT b.id, b.passenger_id, b.driver_id, b.vehicle_id,
               b.pickup_latitude AS pickup_lat, b.pickup_longitude AS pickup_lon,
               COALESCE(b.pickup_address,'') AS pickup_text,
               b.destination_address AS destination_text,
               b.destination_latitude AS destination_lat,
               b.destination_longitude AS destination_lon,
               dl.latitude AS driver_lat, dl.longitude AS driver_lon,
               b.vehicle_type, b.payment_method,
               CASE WHEN b.status='requested' THEN 'searching' ELSE b.status END AS status,
               EXTRACT(EPOCH FROM b.created_at)::BIGINT AS created_at,
               EXTRACT(EPOCH FROM b.updated_at)::BIGINT AS updated_at,
               p.full_name AS passenger_name, duser.full_name AS driver_name
        FROM public.bookings b
        JOIN public.users p ON p.id=b.passenger_id
        LEFT JOIN public.drivers d ON d.id=b.driver_id
        LEFT JOIN public.users duser ON duser.id=d.user_id
        LEFT JOIN public.driver_locations dl ON dl.driver_id=d.id AND dl.is_online=TRUE AND dl.updated_at > NOW() - INTERVAL '2 minutes'
        WHERE {where}
        ORDER BY b.created_at DESC LIMIT 50
    """, params).fetchall()


@app.get("/api/bookings/mine")
def my_bookings(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = get_driver(conn, user["id"])
        if driver and driver["status"] == "approved":
            rows = booking_rows(conn, "(b.passenger_id=%s OR b.driver_id=%s)", (user["id"], driver["id"]))
        else:
            rows = booking_rows(conn, "b.passenger_id=%s", (user["id"],))
    return {"items": [{**dict(r), "id": str(r["id"]), "passenger_id": str(r["passenger_id"]),
                       "driver_id": str(r["driver_id"]) if r["driver_id"] else None} for r in rows]}


@app.get("/api/driver/requests")
def driver_requests(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = require_driver(conn, user["id"])
        rows = booking_rows(conn, "b.status='requested' AND (b.vehicle_type='all' OR b.vehicle_type IN (SELECT v.vehicle_type FROM public.vehicles v WHERE v.driver_id=%s AND v.status='approved'))",
                            (driver["id"],))
    return {"items": [{**dict(r), "id": str(r["id"])} for r in rows]}


@app.post("/api/driver/bookings/{booking_id}/accept")
def accept_booking(booking_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = require_driver(conn, user["id"])
        vehicle = conn.execute("""
            SELECT id FROM public.vehicles WHERE driver_id=%s AND status='approved'
            ORDER BY created_at DESC LIMIT 1
        """, (driver["id"],)).fetchone()
        if not vehicle:
            raise HTTPException(403, "No approved vehicle is registered")
        row = conn.execute("""
            UPDATE public.bookings SET driver_id=%s, vehicle_id=%s, status='accepted', updated_at=NOW()
            WHERE id=%s AND status='requested' AND driver_id IS NULL RETURNING id
        """, (driver["id"], vehicle["id"], booking_id)).fetchone()
        if not row:
            raise HTTPException(409, "This booking is no longer available")
    return {"ok": True, "status": "accepted"}


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = get_driver(conn, user["id"])
        row = conn.execute("SELECT passenger_id,driver_id,status FROM public.bookings WHERE id=%s",
                           (booking_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Booking not found")
        allowed = str(row["passenger_id"]) == str(user["id"]) or (
            driver and row["driver_id"] and str(row["driver_id"]) == str(driver["id"]))
        if not allowed:
            raise HTTPException(403, "Not your booking")
        if row["status"] in ("completed", "cancelled"):
            raise HTTPException(409, "Booking cannot be cancelled")
        conn.execute("UPDATE public.bookings SET status='cancelled',updated_at=NOW() WHERE id=%s",
                     (booking_id,))


@app.post("/api/driver/bookings/{booking_id}/status")
def driver_booking_status(booking_id: str, payload: BookingStatusUpdate,
                          authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    transitions = {"accepted": {"arrived", "cancelled"}, "arrived": {"in_progress", "cancelled"},
                   "in_progress": {"completed", "cancelled"}}
    with db() as conn:
        driver = require_driver(conn, user["id"])
        row = conn.execute("SELECT status FROM public.bookings WHERE id=%s AND driver_id=%s",
                           (booking_id, driver["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Booking not found")
        if payload.status not in transitions.get(row["status"], set()):
            raise HTTPException(409, "Invalid booking status transition")
        conn.execute("UPDATE public.bookings SET status=%s,updated_at=NOW() WHERE id=%s",
                     (payload.status, booking_id))
    return {"ok": True, "status": payload.status}


@app.get("/api/admin/overview")
def admin_overview(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) AS n FROM public.users").fetchone()["n"],
            "pending_drivers": conn.execute("SELECT COUNT(*) AS n FROM public.drivers WHERE status='pending'").fetchone()["n"],
            "active_drivers": conn.execute("SELECT COUNT(*) AS n FROM public.drivers WHERE status='approved'").fetchone()["n"],
            "active_bookings": conn.execute("SELECT COUNT(*) AS n FROM public.bookings WHERE status IN ('requested','accepted','arrived','in_progress')").fetchone()["n"]
        }


@app.get("/api/admin/drivers")
def admin_drivers(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = conn.execute("""
            SELECT d.id AS driver_id, u.telegram_user_id::TEXT AS telegram_id,
                   u.full_name AS first_name, u.username, u.phone,
                   d.status, v.plate_number, v.model AS vehicle_model
            FROM public.drivers d JOIN public.users u ON u.id=d.user_id
            LEFT JOIN LATERAL (
                SELECT plate_number, model FROM public.vehicles
                WHERE driver_id=d.id ORDER BY created_at DESC LIMIT 1
            ) v ON TRUE
            ORDER BY d.created_at DESC LIMIT 200
        """).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/drivers/{driver_id}/approve")
def approve_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        driver = conn.execute("UPDATE public.drivers SET status='approved', reviewed_by=%s, reviewed_at=NOW(), rejection_reason=NULL, updated_at=NOW() WHERE id=%s AND status='pending' RETURNING user_id",
                              (user["id"], driver_id)).fetchone()
        if not driver:
            raise HTTPException(404, "Pending driver not found")
        conn.execute("UPDATE public.users SET role='driver',updated_at=NOW() WHERE id=%s", (driver["user_id"],))
        conn.execute("UPDATE public.vehicles SET status='approved' WHERE driver_id=%s AND status='pending'", (driver_id,))
    return {"ok": True, "status": "approved"}


@app.post("/api/admin/drivers/{driver_id}/reject")
def reject_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        driver = conn.execute("UPDATE public.drivers SET status='rejected', reviewed_by=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s AND status='pending' RETURNING user_id",
                              (user["id"], driver_id)).fetchone()
        if not driver:
            raise HTTPException(404, "Pending driver not found")
        conn.execute("UPDATE public.users SET role='passenger',updated_at=NOW() WHERE id=%s", (driver["user_id"],))
        conn.execute("UPDATE public.vehicles SET status='rejected' WHERE driver_id=%s AND status='pending'", (driver_id,))
    return {"ok": True, "status": "rejected"}


@app.post("/api/admin/users/{target_id}/block")
def block_user(target_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        target = conn.execute("SELECT id,role FROM public.users WHERE telegram_user_id=%s",
                              (int(target_id),)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if target["role"] == "admin":
            raise HTTPException(400, "Cannot block an administrator")
        conn.execute("UPDATE public.users SET account_status='suspended',updated_at=NOW() WHERE id=%s", (target["id"],))
        conn.execute("UPDATE public.drivers SET status='blocked',online_status=FALSE,updated_at=NOW() WHERE user_id=%s", (target["id"],))
    return {"ok": True, "blocked": True}


@app.get("/api/admin/bookings")
def admin_bookings(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = booking_rows(conn, "TRUE", ())
    return {"items": [{**dict(r), "id": str(r["id"]), "passenger_id": str(r["passenger_id"])} for r in rows]}


@app.get("/api/admin/blocked-plates")
def blocked_plates(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = conn.execute("""
            SELECT id, plate_number, model AS vehicle_model, status
            FROM public.vehicles WHERE status='blocked' ORDER BY created_at DESC
        """).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/drivers/{driver_id}/block")
def block_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        driver = conn.execute("UPDATE public.drivers SET status='blocked',online_status=FALSE,updated_at=NOW() WHERE id=%s RETURNING user_id",
                              (driver_id,)).fetchone()
        if not driver:
            raise HTTPException(404, "Driver not found")
        conn.execute("UPDATE public.users SET account_status='suspended',updated_at=NOW() WHERE id=%s",
                     (driver["user_id"],))
        conn.execute("UPDATE public.vehicles SET status='blocked' WHERE driver_id=%s", (driver_id,))
        conn.execute("UPDATE public.driver_locations SET is_online=FALSE,updated_at=NOW() WHERE driver_id=%s",
                     (driver_id,))
    return {"ok": True, "blocked": True}


# Telegram Bot polling: /start opens the WING FAST Mini App.
import asyncio
import urllib.error
import urllib.request

MINI_APP_URL = os.getenv("MINI_APP_URL", "https://topfan-ivpq.onrender.com").strip()


def telegram_api(method: str, payload: dict, timeout: int = 35) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API request failed"))
    return result


def handle_bot_update(update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text_value = (message.get("text") or "").strip()
    if not chat_id or not text_value:
        return
    command = text_value.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        payload = {
            "chat_id": chat_id,
            "text": (
                "សូមស្វាគមន៍មកកាន់ WING FAST! 🚕\\n"
                "កក់ការធ្វើដំណើរ និងចុះឈ្មោះជាអ្នកបើកបរ តាម Mini App ខាងក្រោម។"
            ),
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "🚕 Open WING FAST",
                    "web_app": {"url": MINI_APP_URL}
                }]]
            }
        }
        telegram_api("sendMessage", payload, timeout=15)
    elif command == "/ping":
        telegram_api("sendMessage", {
            "chat_id": chat_id, "text": "WING FAST Bot is online ✅"
        }, timeout=15)


async def telegram_polling_loop():
    if not BOT_TOKEN:
        print("Telegram bot polling disabled: BOT_TOKEN is missing")
        return
    try:
        # Switch this bot to long polling; a bot cannot poll and use a webhook simultaneously.
        await asyncio.to_thread(telegram_api, "deleteWebhook", {"drop_pending_updates": False}, 20)
        me_result = await asyncio.to_thread(telegram_api, "getMe", {}, 15)
        bot_name = (me_result.get("result") or {}).get("username", "unknown")
        print(f"Telegram bot polling started: @{bot_name}")
    except Exception as exc:
        print(f"Telegram bot startup failed: {type(exc).__name__}: {exc}")
        return

    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            result = await asyncio.to_thread(telegram_api, "getUpdates", params, 35)
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    await asyncio.to_thread(handle_bot_update, update)
                except Exception as exc:
                    print(f"Telegram update handling failed: {type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Telegram polling error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)


_bot_polling_task = None


@app.on_event("startup")
async def start_telegram_bot():
    global _bot_polling_task
    if BOT_TOKEN:
        _bot_polling_task = asyncio.create_task(telegram_polling_loop())


@app.on_event("shutdown")
async def stop_telegram_bot():
    global _bot_polling_task
    if _bot_polling_task:
        _bot_polling_task.cancel()
        try:
            await _bot_polling_task
        except asyncio.CancelledError:
            pass
