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
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
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


def clean(value, maximum, label):
    value = value.strip()
    if not value or len(value) > maximum:
        raise HTTPException(422, f"{label} is required and must be under {maximum} characters")
    return value


class DriverApplication(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    vehicle_type: str = Field(pattern="^(car|suv|tuk_tuk|motorbike)$")
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
    vehicle_type: str = Field(default="all", pattern="^(all|car|suv|tuk_tuk|motorbike)$")
    payment_method: str = Field(default="cash", pattern="^(cash|digital)$")


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


@app.post("/api/bookings")
def create_booking(payload: BookingCreate, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    if (payload.destination_lat is None) != (payload.destination_lon is None):
        raise HTTPException(422, "Destination coordinates must include both latitude and longitude")
    with db() as conn:
        row = conn.execute("""
            INSERT INTO public.bookings (
                passenger_id, pickup_latitude, pickup_longitude, pickup_address,
                destination_latitude, destination_longitude, destination_address,
                vehicle_type, payment_method, status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'requested')
            RETURNING id
        """, (user["id"], payload.pickup_lat, payload.pickup_lon,
              payload.pickup_text.strip(), payload.destination_lat, payload.destination_lon,
              clean(payload.destination_text, 180, "Destination"),
              payload.vehicle_type, payload.payment_method)).fetchone()
    return {"ok": True, "booking_id": str(row["id"]), "status": "searching"}


def booking_rows(conn, where, params):
    return conn.execute(f"""
        SELECT b.id, b.passenger_id, b.driver_id, b.vehicle_id,
               b.pickup_latitude AS pickup_lat, b.pickup_longitude AS pickup_lon,
               COALESCE(b.pickup_address,'') AS pickup_text,
               b.destination_address AS destination_text,
               b.destination_latitude AS destination_lat,
               b.destination_longitude AS destination_lon,
               b.vehicle_type, b.payment_method,
               CASE WHEN b.status='requested' THEN 'searching' ELSE b.status END AS status,
               EXTRACT(EPOCH FROM b.created_at)::BIGINT AS created_at,
               EXTRACT(EPOCH FROM b.updated_at)::BIGINT AS updated_at,
               p.full_name AS passenger_name, duser.full_name AS driver_name
        FROM public.bookings b
        JOIN public.users p ON p.id=b.passenger_id
        LEFT JOIN public.drivers d ON d.id=b.driver_id
        LEFT JOIN public.users duser ON duser.id=d.user_id
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
