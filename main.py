import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

APP_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(APP_DIR / "wingfast.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

app = FastAPI(title="WING FAST API", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR)), name="static")


SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()


class CompatRow(dict):
    """Dictionary row that also supports SQLite-style numeric indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class CompatCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self):
        return [CompatRow(row) for row in self.cursor.fetchall()]


class PostgresCompatConnection:
    """Small adapter so existing parameterized SQL works with Supabase Postgres."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("PRAGMA"):
            return CompatCursor(self.connection.execute("SELECT 1 AS ok"))
        return CompatCursor(self.connection.execute(sql.replace("?", "%s"), params))

    def executescript(self, script):
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


@contextmanager
def db():
    if SUPABASE_DB_URL:
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(SUPABASE_DB_URL, row_factory=dict_row, connect_timeout=15)
        conn = PostgresCompatConnection(raw)
    else:
        raw = sqlite3.connect(DB_PATH, timeout=20)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA journal_mode = WAL")
        conn = raw
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    if not SUPABASE_DB_URL:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'passenger',
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drivers (
            telegram_id TEXT PRIMARY KEY REFERENCES users(telegram_id),
            phone TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            vehicle_model TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY,
            passenger_id TEXT NOT NULL REFERENCES users(telegram_id),
            driver_id TEXT REFERENCES users(telegram_id),
            pickup_lat REAL NOT NULL,
            pickup_lon REAL NOT NULL,
            pickup_text TEXT NOT NULL DEFAULT '',
            destination_text TEXT NOT NULL,
            destination_lat REAL,
            destination_lon REAL,
            vehicle_type TEXT NOT NULL DEFAULT 'all',
            payment_method TEXT NOT NULL DEFAULT 'cash',
            fare_note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'searching',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS driver_locations (
            telegram_id TEXT PRIMARY KEY REFERENCES users(telegram_id),
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            accuracy REAL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bookings_passenger ON bookings(passenger_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bookings_driver ON bookings(driver_id, status);
        CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status, created_at);
        """)
init_db()


def verify_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(503, "Server is not configured: BOT_TOKEN is missing")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = pairs.pop("hash", "")
        auth_date = int(pairs.get("auth_date", "0"))
        if not received_hash or not auth_date:
            raise ValueError("Missing Telegram signature")
        if abs(int(time.time()) - auth_date) > INIT_DATA_MAX_AGE:
            raise HTTPException(401, "Telegram session expired. Reopen the Mini App.")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            raise ValueError("Invalid Telegram signature")
        user = json.loads(pairs["user"])
        if not user.get("id"):
            raise ValueError("Missing Telegram user ID")
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid Telegram authentication data")


def current_user(authorization: Optional[str]) -> dict:
    if DEV_MODE and not BOT_TOKEN:
        # Local-only test identity. Never enable DEV_MODE on a public deployment.
        return {"id": 999000001, "first_name": "Local Tester", "username": "local_test"}
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Open WING FAST inside Telegram")
    return verify_telegram_init_data(authorization[4:].strip())


def user_id(user: dict) -> str:
    return str(user["id"])


def ensure_user(conn, tg_user: dict):
    uid = user_id(tg_user)
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    first = str(tg_user.get("first_name", ""))[:80]
    username = str(tg_user.get("username", ""))[:80]
    if row is None:
        role = "admin" if uid in ADMIN_IDS else "passenger"
        conn.execute(
            "INSERT INTO users(telegram_id,first_name,username,role,created_at) VALUES(?,?,?,?,?)",
            (uid, first, username, role, int(time.time()))
        )
    else:
        # Admin privileges are granted only by the server-side ADMIN_IDS allowlist.
        role = "admin" if uid in ADMIN_IDS else ("passenger" if row["role"] == "admin" else row["role"])
        conn.execute(
            "UPDATE users SET first_name=?, username=?, role=? WHERE telegram_id=?",
            (first, username, role, uid)
        )
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    if row["blocked"]:
        raise HTTPException(403, "Your account is blocked. Contact WING FAST support.")
    return dict(row)


def auth_user(authorization: Optional[str]):
    tg = current_user(authorization)
    with db() as conn:
        return ensure_user(conn, tg)


def require_admin(user: dict):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")


def require_driver(conn, uid: str):
    row = conn.execute("SELECT * FROM drivers WHERE telegram_id=?", (uid,)).fetchone()
    if not row or row["status"] != "approved":
        raise HTTPException(403, "Driver account is not approved")
    return row


def clean_text(value: str, maximum: int, label: str) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise HTTPException(422, f"{label} is required and must be under {maximum} characters")
    return value


class DriverApplication(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    vehicle_type: str = Field(min_length=2, max_length=30)
    vehicle_model: str = Field(min_length=2, max_length=80)
    plate_number: str = Field(min_length=2, max_length=30)

    @field_validator("phone", "vehicle_type", "vehicle_model", "plate_number")
    @classmethod
    def strip_fields(cls, value):
        return value.strip()


class BookingCreate(BaseModel):
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lon: float = Field(ge=-180, le=180)
    pickup_text: str = Field(default="", max_length=180)
    destination_text: str = Field(min_length=2, max_length=180)
    destination_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    destination_lon: Optional[float] = Field(default=None, ge=-180, le=180)
    vehicle_type: str = Field(default="all", max_length=30)
    payment_method: str = Field(default="cash", pattern="^(cash|digital)$")


class LocationUpdate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = Field(default=None, ge=0, le=100000)


class BookingStatusUpdate(BaseModel):
    status: str = Field(pattern="^(accepted|arrived|in_progress|completed|cancelled)$")


@app.get("/")
def home():
    return FileResponse(APP_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "app": "WING FAST", "version": app.version}


@app.get("/api/me")
def me(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        driver = conn.execute("SELECT status,vehicle_type,vehicle_model,plate_number FROM drivers WHERE telegram_id=?",
                              (user["telegram_id"],)).fetchone()
    return {"user": user, "driver": dict(driver) if driver else None}


@app.post("/api/driver/apply")
def driver_apply(payload: DriverApplication, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    uid = user["telegram_id"]
    with db() as conn:
        blocked = conn.execute(
            "SELECT 1 FROM drivers WHERE lower(plate_number)=lower(?) AND telegram_id<>? AND status='blocked'",
            (payload.plate_number, uid)
        ).fetchone()
        if blocked:
            raise HTTPException(403, "This vehicle plate is blocked")
        duplicate = conn.execute(
            "SELECT 1 FROM drivers WHERE lower(plate_number)=lower(?) AND telegram_id<>? AND status IN ('pending','approved')",
            (payload.plate_number, uid)
        ).fetchone()
        if duplicate:
            raise HTTPException(409, "This license plate is already registered")
        conn.execute("""
            INSERT INTO drivers(telegram_id,phone,vehicle_type,vehicle_model,plate_number,status,updated_at)
            VALUES(?,?,?,?,?,'pending',?)
            ON CONFLICT(telegram_id) DO UPDATE SET
            phone=excluded.phone, vehicle_type=excluded.vehicle_type,
            vehicle_model=excluded.vehicle_model, plate_number=excluded.plate_number,
            status='pending', admin_note='', updated_at=excluded.updated_at
        """, (uid, payload.phone, payload.vehicle_type, payload.vehicle_model,
              payload.plate_number.upper(), int(time.time())))
        conn.execute("UPDATE users SET role='driver_pending' WHERE telegram_id=?", (uid,))
    return {"ok": True, "status": "pending", "message": "Application submitted for admin review"}


@app.post("/api/driver/location")
def driver_location(payload: LocationUpdate, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    uid = user["telegram_id"]
    with db() as conn:
        require_driver(conn, uid)
        now = int(time.time())
        conn.execute("""
            INSERT INTO driver_locations(telegram_id,lat,lon,accuracy,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET lat=excluded.lat,lon=excluded.lon,
            accuracy=excluded.accuracy,updated_at=excluded.updated_at
        """, (uid, payload.lat, payload.lon, payload.accuracy, now))
    return {"ok": True, "updated_at": now}


@app.post("/api/bookings")
def create_booking(payload: BookingCreate, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    if payload.destination_lat is None and payload.destination_lon is not None:
        raise HTTPException(422, "Destination coordinates must include both latitude and longitude")
    if payload.destination_lon is None and payload.destination_lat is not None:
        raise HTTPException(422, "Destination coordinates must include both latitude and longitude")
    booking_id = secrets.token_urlsafe(9)
    now = int(time.time())
    with db() as conn:
        conn.execute("""
            INSERT INTO bookings(id,passenger_id,pickup_lat,pickup_lon,pickup_text,destination_text,
            destination_lat,destination_lon,vehicle_type,payment_method,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (booking_id, user["telegram_id"], payload.pickup_lat, payload.pickup_lon,
              payload.pickup_text.strip(), clean_text(payload.destination_text, 180, "Destination"),
              payload.destination_lat, payload.destination_lon, payload.vehicle_type,
              payload.payment_method, now, now))
    return {"ok": True, "booking_id": booking_id, "status": "searching"}


@app.get("/api/bookings/mine")
def my_bookings(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    uid = user["telegram_id"]
    with db() as conn:
        if user["role"] in ("driver", "driver_pending"):
            rows = conn.execute("""
                SELECT b.*,u.first_name AS passenger_name FROM bookings b
                JOIN users u ON u.telegram_id=b.passenger_id
                WHERE b.driver_id=? OR (b.status='searching' AND ?='driver')
                ORDER BY b.created_at DESC LIMIT 50
            """, (uid, user["role"])).fetchall()
        else:
            rows = conn.execute("""
                SELECT b.*,u.first_name AS driver_name FROM bookings b
                LEFT JOIN users u ON u.telegram_id=b.driver_id
                WHERE b.passenger_id=? ORDER BY b.created_at DESC LIMIT 50
            """, (uid,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/driver/requests")
def driver_requests(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        require_driver(conn, user["telegram_id"])
        rows = conn.execute("""
            SELECT b.*,u.first_name AS passenger_name FROM bookings b
            JOIN users u ON u.telegram_id=b.passenger_id
            WHERE b.status='searching' ORDER BY b.created_at DESC LIMIT 30
        """).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/driver/bookings/{booking_id}/accept")
def accept_booking(booking_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    uid = user["telegram_id"]
    with db() as conn:
        require_driver(conn, uid)
        cur = conn.execute("""
            UPDATE bookings SET driver_id=?,status='accepted',updated_at=?
            WHERE id=? AND status='searching' AND driver_id IS NULL
        """, (uid, int(time.time()), booking_id))
        if cur.rowcount != 1:
            raise HTTPException(409, "This booking is no longer available")
    return {"ok": True, "status": "accepted"}


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    with db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Booking not found")
        if row["passenger_id"] != user["telegram_id"] and row["driver_id"] != user["telegram_id"]:
            raise HTTPException(403, "Not your booking")
        if row["status"] in ("completed", "cancelled"):
            raise HTTPException(409, "Booking cannot be cancelled")
        conn.execute("UPDATE bookings SET status='cancelled',updated_at=? WHERE id=?",
                     (int(time.time()), booking_id))
    return {"ok": True, "status": "cancelled"}


@app.post("/api/driver/bookings/{booking_id}/status")
def driver_booking_status(booking_id: str, payload: BookingStatusUpdate,
                          authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    allowed = {"accepted": {"arrived", "cancelled"}, "arrived": {"in_progress", "cancelled"},
               "in_progress": {"completed", "cancelled"}}
    with db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Booking not found")
        if row["driver_id"] != user["telegram_id"]:
            raise HTTPException(403, "Not your booking")
        if payload.status not in allowed.get(row["status"], set()):
            raise HTTPException(409, "Invalid booking status transition")
        conn.execute("UPDATE bookings SET status=?,updated_at=? WHERE id=?",
                     (payload.status, int(time.time()), booking_id))
    return {"ok": True, "status": payload.status}


@app.get("/api/admin/overview")
def admin_overview(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "pending_drivers": conn.execute("SELECT COUNT(*) FROM drivers WHERE status='pending'").fetchone()[0],
            "active_drivers": conn.execute("SELECT COUNT(*) FROM drivers WHERE status='approved'").fetchone()[0],
            "active_bookings": conn.execute("SELECT COUNT(*) FROM bookings WHERE status IN ('searching','accepted','arrived','in_progress')").fetchone()[0]
        }


@app.get("/api/admin/drivers")
def admin_drivers(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = conn.execute("""
            SELECT d.*,u.first_name,u.username FROM drivers d
            JOIN users u ON u.telegram_id=d.telegram_id ORDER BY d.updated_at DESC LIMIT 200
        """).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/drivers/{driver_id}/approve")
def approve_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        cur = conn.execute("UPDATE drivers SET status='approved',admin_note='',updated_at=? WHERE telegram_id=? AND status='pending'",
                           (int(time.time()), driver_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Pending driver not found")
        conn.execute("UPDATE users SET role='driver' WHERE telegram_id=?", (driver_id,))
    return {"ok": True, "status": "approved"}


@app.post("/api/admin/drivers/{driver_id}/reject")
def reject_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        cur = conn.execute("UPDATE drivers SET status='rejected',updated_at=? WHERE telegram_id=? AND status='pending'",
                           (int(time.time()), driver_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Pending driver not found")
        conn.execute("UPDATE users SET role='passenger' WHERE telegram_id=?", (driver_id,))
    return {"ok": True, "status": "rejected"}


@app.post("/api/admin/users/{target_id}/block")
def block_user(target_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    if target_id in ADMIN_IDS:
        raise HTTPException(400, "Cannot block an administrator")
    with db() as conn:
        cur = conn.execute("UPDATE users SET blocked=1 WHERE telegram_id=?", (target_id,))
        conn.execute("UPDATE drivers SET status='blocked',updated_at=? WHERE telegram_id=?",
                     (int(time.time()), target_id))
        conn.execute("UPDATE bookings SET status='cancelled',updated_at=? WHERE driver_id=? AND status IN ('accepted','arrived','in_progress')",
                     (int(time.time()), target_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "User not found")
    return {"ok": True, "blocked": True}


@app.get("/api/admin/bookings")
def admin_bookings(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = conn.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT 200").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/admin/blocked-plates")
def blocked_plates(authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    with db() as conn:
        rows = conn.execute("SELECT telegram_id,plate_number,vehicle_model,status FROM drivers WHERE status='blocked' ORDER BY updated_at DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/drivers/{driver_id}/block")
def block_driver(driver_id: str, authorization: Optional[str] = Header(default=None)):
    user = auth_user(authorization)
    require_admin(user)
    if driver_id in ADMIN_IDS:
        raise HTTPException(400, "Cannot block an administrator")
    with db() as conn:
        cur = conn.execute("UPDATE drivers SET status='blocked',updated_at=? WHERE telegram_id=?",
                           (int(time.time()), driver_id))
        conn.execute("UPDATE users SET blocked=1 WHERE telegram_id=?", (driver_id,))
        conn.execute("UPDATE bookings SET status='cancelled',updated_at=? WHERE driver_id=? AND status IN ('accepted','arrived','in_progress')",
                     (int(time.time()), driver_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Driver not found")
    return {"ok": True, "blocked": True}
