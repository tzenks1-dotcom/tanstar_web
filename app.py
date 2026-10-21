"""
TanstarSafeHousing — a Gweru, Zimbabwe room rental web app.

Run with:
    pip install flask flask-socketio
    python app.py
Then open http://127.0.0.1:5000

Data is stored in a local SQLite file (tanstar.db), created and
seeded automatically the first time the app runs. A demo landlord
account is seeded too — email demo@tanstarsafehousing.co.zw,
password demo1234 — so you can log in immediately and see the
seeded listings on the dashboard.

Map tiles, address search, the routing preview, and the live-update
socket connection all call free public services or the Flask-SocketIO
server from the *browser*, so a real internet connection is needed on
the machine viewing the site for those specific features. If that
connection drops, the app falls back to manual coordinate entry and a
plain offline banner rather than failing outright — see the
"online"/"offline" handling in layout.html.

Prices are quoted in USD, which is the currency most room and cottage
rentals are advertised in across Zimbabwe.
"""

import json
import os
import sqlite3
import uuid
from datetime import date
from functools import wraps

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tanstar.db")

app = Flask(__name__)
app.secret_key = "tanstar-dev-secret"  # replace with a real secret before deploying

# threading mode needs no extra async library (no eventlet/gevent) — fine for
# local dev and small-scale real use; swap async_mode for real production scale.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

DEMO_LANDLORD_EMAIL = "demo@tanstarsafehousing.co.zw"
DEMO_LANDLORD_PASSWORD = "demo1234"
ADMIN_EMAIL = "admin@tanstarsafehousing.co.zw"
ADMIN_PASSWORD = "admin1234"  # change this before going anywhere near production

UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # 6 MB per upload
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}

ROOM_TYPES = ["Private room", "Shared room", "Studio", "Boarding house", "Whole house"]
STATUSES = ["Submitted", "Under review", "Approved", "Declined"]
SAFETY_OPTIONS = [
    "ID-verified landlord", "Perimeter wall & gate", "Prepaid electricity meter",
    "Borehole / water backup", "Neighbourhood watch", "Deadbolt + burglar bars",
    "24-hour security guard", "Background-checked residents", "Fire extinguisher on site",
    "Exterior security lighting",
]

# All listings are in Gweru, Zimbabwe — id, title, suburb, address, type, price (US$),
# size_sqft, available, description, safety, verified, lat, lng, photo_url, landlord_name
SEED_ROOMS = [
    ("rm_gweru_ridgemont", "Room in Ridgemont", "Gweru", "16 Bristol Rd, Ridgemont",
     "Private room", 130, 120, "Sep 30",
     "Central Gweru location, five minutes from the CBD, secure fenced yard shared with "
     "one other tenant.",
     ["Neighbourhood watch", "Deadbolt + burglar bars"], 0,
     -19.4553, 29.8083, "", "Tapiwa Z."),
    ("rm_gweru_mkoba14", "Room in Mkoba 14", "Gweru", "14 Fidel Castro Way, Mkoba 14",
     "Private room", 95, 110, "Oct 1",
     "Room in a family stand in Mkoba 14, close to the shopping centre and kombi rank, "
     "shared kitchen with the landlord's family.",
     ["ID-verified landlord", "Perimeter wall & gate", "Prepaid electricity meter"], 1,
     -19.4716, 29.7850, "", "Mai Moyo"),
    ("rm_gweru_mkoba1", "Shared room, Mkoba 1", "Gweru", "Stand 221, Mkoba 1",
     "Shared room", 65, 100, "Immediately",
     "Simple shared room popular with Midlands State University students, shared borehole "
     "with the main house, five-minute walk to the MSU Main Campus turn-off.",
     ["Background-checked residents", "Borehole / water backup"], 0,
     -19.4625, 29.7930, "", "Mkoba Student Digs"),
    ("rm_gweru_senga", "Cottage in Senga", "Gweru", "9 Robertson Rd, Senga",
     "Studio", 220, 240, "Oct 5",
     "Self-contained garden cottage on a secure Senga property, separate entrance, "
     "landlord lives in the main house.",
     ["ID-verified landlord", "Perimeter wall & gate", "Prepaid electricity meter"], 1,
     -19.4396, 29.8260, "", "Mr. Ncube"),
    ("rm_gweru_mambo", "Room in Mambo", "Gweru", "22 Mambo Drive",
     "Private room", 110, 125, "Oct 10",
     "Room in a quiet Mambo suburb household, off-street parking, close to Gweru Girls High "
     "School.",
     ["ID-verified landlord", "Exterior security lighting"], 0,
     -19.4460, 29.8020, "", "Mrs. Chikwava"),
    ("rm_gweru_ascot", "Bedsitter in Ascot", "Gweru", "5 Ascot Close",
     "Studio", 160, 150, "Sep 28",
     "Affordable self-contained bedsitter in Ascot, prepaid electricity meter, landlord "
     "lives on the same stand, walking distance to Ascot Shopping Centre.",
     ["ID-verified landlord", "Prepaid electricity meter", "Deadbolt + burglar bars"], 1,
     -19.4280, 29.8190, "", "Mrs. Nyoni"),
    ("rm_gweru_windsorpark", "Whole cottage, Windsor Park", "Gweru", "11 Windsor Park Rd",
     "Whole house", 380, 450, "Nov 1",
     "Small two-bedroom cottage in leafy Windsor Park, fenced yard, borehole backup, "
     "popular with young families.",
     ["Perimeter wall & gate", "Borehole / water backup", "Exterior security lighting"], 1,
     -19.4160, 29.8330, "", "Farai C."),
    ("rm_gweru_nashville", "Room in Nashville", "Gweru", "8 Nashville Rd",
     "Private room", 100, 115, "Oct 8",
     "Room in a working-class Nashville household, walled yard, gate guard in the "
     "evenings, close to the industrial sites.",
     ["Neighbourhood watch", "Perimeter wall & gate"], 0,
     -19.4610, 29.8370, "", "Mr. Sibanda"),
    ("rm_gweru_athlone", "Shared room in Athlone", "Gweru", "3 Athlone Ave",
     "Shared room", 70, 105, "Sep 26",
     "Shared house near Guinea Fowl School, two housemates, communal borehole and water "
     "tank for city supply cuts.",
     ["Background-checked residents", "Borehole / water backup"], 0,
     -19.4660, 29.8130, "", "Athlone House"),
    ("rm_gweru_northlea", "Studio in Northlea", "Gweru", "17 Northlea Dr",
     "Studio", 190, 160, "Oct 12",
     "Tidy self-contained studio in Northlea, prepaid meter, quiet street close to "
     "Chaplin High School.",
     ["ID-verified landlord", "Prepaid electricity meter", "Deadbolt + burglar bars"], 1,
     -19.4340, 29.8010, "", "Chipo R."),
    ("rm_gweru_woodlands", "Room in Woodlands", "Gweru", "6 Woodlands Close",
     "Private room", 115, 120, "Oct 3",
     "Room in a quiet Woodlands family home, secure fenced yard, five-minute drive to the "
     "CBD.",
     ["ID-verified landlord", "Deadbolt + burglar bars"], 0,
     -19.4200, 29.8080, "", "Woodlands Family Home"),
    ("rm_gweru_clipsham", "Shared room, Clipsham Park", "Gweru", "14 Clipsham Park Rd",
     "Shared room", 75, 100, "Sep 29",
     "Simple shared room close to Clipsham Park shops, borehole water backup, shared "
     "kitchen with two other tenants.",
     ["Borehole / water backup", "Background-checked residents"], 0,
     -19.4480, 29.8460, "", "Clipsham Household"),
    ("rm_gweru_lundipark", "Cottage in Lundi Park", "Gweru", "19 Lundi Park Rd",
     "Studio", 240, 210, "Oct 15",
     "Self-contained cottage on the edge of Lundi Park, easy drive to town along Robert "
     "Mugabe Way, prepaid meter installed.",
     ["Prepaid electricity meter", "Perimeter wall & gate", "Exterior security lighting"], 1,
     -19.4030, 29.8150, "", "Lundi Park Cottages"),
    ("rm_gweru_southdale", "Studio in Southdale", "Gweru", "4 Southdale Rd",
     "Studio", 200, 170, "Oct 6",
     "Self-contained studio in a quiet Southdale household, secure yard, prepaid "
     "electricity meter, close to the Gweru Golf Club.",
     ["Prepaid electricity meter", "Perimeter wall & gate"], 1,
     -19.4710, 29.8220, "", "Southdale Homestead"),
    ("rm_gweru_daylesford", "Room in Daylesford", "Gweru", "21 Daylesford Rd",
     "Private room", 90, 100, "Sep 25",
     "Basic room close to Daylesford shops, shared borehole with the main house, friendly "
     "landlady on the property.",
     ["ID-verified landlord", "Borehole / water backup"], 0,
     -19.4540, 29.7960, "", "Daylesford Household"),
    ("rm_gweru_cbd", "Studio near Gweru CBD", "Gweru", "2 Main St, Gweru Town Centre",
     "Studio", 260, 180, "Nov 1",
     "Self-contained studio above a shop right in the Gweru CBD, walking distance to banks, "
     "the bus terminus, and Jacaranda Shopping Centre.",
     ["24-hour security guard", "Deadbolt + burglar bars"], 1,
     -19.4500, 29.8167, "", "CBD Residences"),
    ("rm_gweru_clonsilla", "Room in Clonsilla", "Gweru", "8 Clonsilla Rd",
     "Private room", 105, 115, "Oct 9",
     "Room in a quiet Clonsilla household, fenced yard, close to Midlands State University "
     "and the Gweru showgrounds.",
     ["ID-verified landlord", "Perimeter wall & gate"], 1,
     -19.4610, 29.8480, "", "Clonsilla Family Home"),
    ("rm_gweru_shamrock", "Shared room in Shamrock Park", "Gweru", "13 Shamrock Park Rd",
     "Shared room", 72, 105, "Sep 24",
     "Shared house popular with civil servants, two housemates, prepaid electricity meter.",
     ["Prepaid electricity meter", "Background-checked residents"], 0,
     -19.4370, 29.7900, "", "Shamrock Household"),
    # Extra density around Senga / MSU Main Campus (where most students start
    # looking), plus Mkoba, Woodlands and a few more suburbs. Sample data.
    ("rm_gweru_senga2", "Shared room, two minutes from MSU gate", "Gweru",
     "3 Mkoba Drive, Senga", "Shared room", 75, 100, "Oct 1",
     "Student house right by the Midlands State University Main Campus gate, five "
     "students currently, rotating cleaning roster, popular with first-years.",
     ["Background-checked residents", "Deadbolt + burglar bars"], 1,
     -19.4410, 29.8295, "", "Senga Student House"),
    ("rm_gweru_senga3", "Private room near MSU Main Campus", "Gweru", "17 Robertson Rd, Senga",
     "Private room", 125, 115, "Oct 8",
     "Quiet private room a short walk from the MSU Main Campus library, landlord rents "
     "mainly to students.",
     ["ID-verified landlord", "Perimeter wall & gate", "Prepaid electricity meter"], 1,
     -19.4385, 29.8310, "", "Mr. Mutasa"),
    ("rm_gweru_senga4", "Bedsitter in Senga, walk to campus", "Gweru", "25 Senga Rd",
     "Studio", 165, 140, "Sep 27",
     "Self-contained bedsitter popular with postgraduate students, ten-minute walk to "
     "the MSU Main Campus entrance, prepaid electricity meter.",
     ["Prepaid electricity meter", "Deadbolt + burglar bars"], 0,
     -19.4420, 29.8240, "", "Senga Lodgings"),
    ("rm_gweru_mkoba6", "Room in Mkoba 6", "Gweru", "Stand 88, Mkoba 6",
     "Private room", 90, 110, "Oct 4",
     "Room in a family stand close to Mkoba 6 shopping centre, regular kombis to town "
     "and to MSU.",
     ["ID-verified landlord", "Perimeter wall & gate"], 0,
     -19.4580, 29.7980, "", "Mai Ndlovu"),
    ("rm_gweru_woodlands2", "Shared room in Woodlands", "Gweru", "10 Woodlands Rd",
     "Shared room", 85, 100, "Oct 2",
     "Shared house in Woodlands with two working tenants, quiet street, borehole backup "
     "for city water cuts.",
     ["Borehole / water backup", "Background-checked residents"], 1,
     -19.4225, 29.8095, "", "Woodlands Household"),
    ("rm_gweru_mtapa", "Room in Mtapa", "Gweru", "6 Mtapa Shopping Centre Rd",
     "Private room", 80, 105, "Sep 26",
     "Affordable room close to Mtapa shops and the main Gweru-Bulawayo road, shared "
     "kitchen with the landlord's family.",
     ["ID-verified landlord", "Borehole / water backup"], 0,
     -19.4780, 29.7760, "", "Mtapa Family Home"),
    ("rm_gweru_nehosho", "Shared room in Nehosho", "Gweru", "4 Nehosho Rd",
     "Shared room", 78, 100, "Oct 7",
     "Simple shared room in Nehosho, close to the industrial sites, borehole on the "
     "property, two other tenants.",
     ["Borehole / water backup", "Background-checked residents"], 0,
     -19.4530, 29.7820, "", "Nehosho Household"),
    # Boarding houses (a bed in a shared room, usually with house rules and a warden)
    ("rm_bh_senga1", "Kudzai Boarding House, Senga", "Gweru", "8 Senga Rd, Senga",
     "Boarding house", 55, 90, "Immediately",
     "Student boarding house a few minutes' walk from the MSU Main Campus. Beds in four-person "
     "rooms, shared bathrooms, house rules and a resident warden.",
     ["ID-verified landlord", "Perimeter wall & gate", "Background-checked residents"], 1,
     -19.4402, 29.8268, "", "Mrs. Kudzai Moyo"),
    ("rm_bh_senga2", "Gate Road Boarding House, Senga", "Gweru", "21 Gate Rd, Senga",
     "Boarding house", 65, 95, "Oct 1",
     "Two-person rooms, prepaid electricity, borehole water, study room open until 10pm.",
     ["Prepaid electricity meter", "Borehole / water backup", "Deadbolt + burglar bars"], 1,
     -19.4372, 29.8338, "", "Mr. Tafara Chikomo"),
    ("rm_bh_mkoba8", "Mkoba 8 Boarding House", "Gweru", "Stand 412, Mkoba 8",
     "Boarding house", 50, 90, "Oct 3",
     "Affordable beds in shared rooms, kombi route to town and MSU, communal kitchen.",
     ["ID-verified landlord", "Perimeter wall & gate"], 0,
     -19.4650, 29.7890, "", "Mai Sibanda"),
    ("rm_bh_woodlands1", "Woodlands Student Lodge", "Gweru", "12 Woodlands Close",
     "Boarding house", 70, 100, "Oct 6",
     "Quiet boarding house for students and young workers, two-person rooms, secure yard and "
     "evening gate guard.",
     ["24-hour security guard", "Perimeter wall & gate", "Background-checked residents"], 1,
     -19.4215, 29.8105, "", "Woodlands Lodge Trust"),
]

# id, renter_name, email, phone, lat, lng, area_label, type, budget, move_in, note
SEED_REQUESTS = [
    ("rq_demo1", "Tapiwa N.", "tapiwa.demo@example.com", "", -19.4500, 29.8167,
     "Gweru CBD or nearby", "Private room", 110, "Oct 15",
     "Need something walled and secure, prefer prepaid electricity."),
    ("rq_demo2", "Linda M.", "linda.demo@example.com", "", -19.4396, 29.8260,
     "Senga or Ascot, Gweru", "Studio", 200, "Nov 1",
     "Working professional, quiet household preferred."),
    ("rq_demo3", "Brian K.", "brian.demo@example.com", "", -19.4625, 29.7930,
     "Mkoba, near MSU", "Shared room", 70, "Immediately",
     "Student at Midlands State University, flexible on exact village."),
    ("rq_demo4", "Rutendo S.", "rutendo.demo@example.com", "", -19.4400, 29.8280,
     "Senga, as close to MSU Main Campus as possible", "Boarding house", 60, "Oct 1",
     "First-year MSU student, would love walking distance to the Main Campus gate."),
]

# Restaurants / eateries — id, name, owner, cuisine, address, price range (US$),
# description, lat, lng, photo_url. Sample data: admins add the real ones.
SEED_PLACES = [
    ("pl_campus_grill", "Campus Corner Grill", "Mr. Tendai Chuma", "Grills & braai",
     "Senga Rd, near the MSU gate", "3-6", "Grilled chicken, sadza and braai plates, "
     "popular with students at lunchtime.", -19.4408, 29.8288, ""),
    ("pl_mama_rudo", "Mama Rudo's Kitchen", "Mai Rudo", "Traditional (sadza & stews)",
     "Mkoba 1 shopping centre", "2-4", "Home-style sadza with beef stew, covo and "
     "beans.", -19.4628, 29.7935, ""),
    ("pl_senga_chicken", "Senga Chicken Hub", "Mr. Blessing Dube", "Fried chicken & chips",
     "Senga shops", "3-5", "Fried chicken, chips and cold drinks, open late.",
     -19.4392, 29.8255, ""),
    ("pl_woodlands_cafe", "Woodlands Cafe", "Mrs. Patience Moyo", "Breakfast & coffee",
     "Woodlands shopping centre", "2-6", "Cooked breakfasts, tea, coffee and light "
     "lunches.", -19.4222, 29.8090, ""),
    ("pl_cbd_burgers", "Gweru Pizza & Burgers", "Mr. Farai Nyathi", "Pizza & burgers",
     "Main St, Gweru CBD", "4-8", "Pizzas, burgers and milkshakes in the town centre.",
     -19.4505, 29.8160, ""),
    ("pl_ascot_braai", "Ascot Braai Spot", "Mr. Simba Gumbo", "Braai",
     "Ascot shopping centre", "3-7", "Evening braai with meat platters and sadza.",
     -19.4283, 29.8186, ""),
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_column(db, table, column, coltype):
    """Add a column to an existing table if it's missing — lets an older
    tanstar.db from a previous version of this app upgrade in place instead
    of crashing on a missing column."""
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    first_run = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS landlords (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT NOT NULL,
            type TEXT NOT NULL,
            price INTEGER NOT NULL,
            size_sqft INTEGER NOT NULL,
            available TEXT NOT NULL,
            description TEXT NOT NULL,
            safety TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            photo_url TEXT,
            landlord_name TEXT,
            landlord_id TEXT,
            posted_at TEXT
        )
    """)
    ensure_column(db, "rooms", "landlord_id", "TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            applicant_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            move_in TEXT NOT NULL,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'Submitted',
            submitted_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms (id)
        )
    """)
    # "Request a room" = the inDrive-style trip request: a renter drops a pin and
    # names their own price instead of picking a listing first.
    db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            renter_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            area_label TEXT,
            type TEXT NOT NULL,
            budget INTEGER NOT NULL,
            move_in TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'Open',
            created_at TEXT NOT NULL
        )
    """)
    # Offers = a landlord either matching the renter's price or countering —
    # same shape as a driver accepting or re-quoting a ride request.
    db.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            landlord_name TEXT NOT NULL,
            landlord_id TEXT,
            price INTEGER NOT NULL,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (request_id) REFERENCES requests (id),
            FOREIGN KEY (room_id) REFERENCES rooms (id)
        )
    """)
    ensure_column(db, "offers", "landlord_id", "TEXT")

    ensure_column(db, "landlords", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    db.execute("""
        CREATE TABLE IF NOT EXISTS places (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_name TEXT,
            cuisine TEXT,
            address TEXT NOT NULL,
            price_range TEXT,
            description TEXT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            photo_url TEXT,
            posted_at TEXT
        )
    """)

    # Seeding is idempotent so both fresh and upgraded databases get the sample data.
    demo = db.execute("SELECT id FROM landlords WHERE email = ?", (DEMO_LANDLORD_EMAIL,)).fetchone()
    if demo:
        demo_landlord_id = demo[0]
    else:
        demo_landlord_id = "ll_" + str(uuid.uuid4())[:10]
        db.execute(
            "INSERT INTO landlords (id, name, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (demo_landlord_id, "Demo Landlord", DEMO_LANDLORD_EMAIL,
             generate_password_hash(DEMO_LANDLORD_PASSWORD), str(date.today())),
        )
    if not db.execute("SELECT id FROM landlords WHERE is_admin = 1").fetchone():
        db.execute(
            "INSERT OR IGNORE INTO landlords (id, name, email, password_hash, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("ll_admin_" + str(uuid.uuid4())[:6], "Tanstar Admin", ADMIN_EMAIL,
             generate_password_hash(ADMIN_PASSWORD), str(date.today())),
        )
    for r in SEED_ROOMS:
        (rid, title, city, address, rtype, price, size_sqft, available, description,
         safety, verified, lat, lng, photo_url, landlord_name) = r
        db.execute(
            "INSERT OR IGNORE INTO rooms (id, title, city, address, type, price, size_sqft, "
            "available, description, safety, verified, lat, lng, photo_url, "
            "landlord_name, landlord_id, posted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, title, city, address, rtype, price, size_sqft, available, description,
             json.dumps(safety), verified, lat, lng, photo_url, landlord_name,
             demo_landlord_id, str(date.today())),
        )
    for rq in SEED_REQUESTS:
        (rqid, renter_name, email, phone, lat, lng, area_label, rtype, budget,
         move_in, note) = rq
        db.execute(
            "INSERT OR IGNORE INTO requests (id, renter_name, email, phone, lat, lng, "
            "area_label, type, budget, move_in, note, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rqid, renter_name, email, phone, lat, lng, area_label, rtype, budget,
             move_in, note, "Open", str(date.today())),
        )
    for pl in SEED_PLACES:
        db.execute(
            "INSERT OR IGNORE INTO places (id, name, owner_name, cuisine, address, price_range, "
            "description, lat, lng, photo_url, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            pl + (str(date.today()),),
        )
    db.commit()
    db.close()


def haversine_km(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def room_to_dict(row):
    d = dict(row)
    d["safety"] = json.loads(d["safety"])
    return d


# --------------------------------------------------------------------------
# Landlord accounts
# --------------------------------------------------------------------------

def current_landlord():
    if "landlord_id" not in session:
        return None
    return {"id": session["landlord_id"], "name": session.get("landlord_name", "")}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "landlord_id" not in session:
            return redirect(url_for("landlord_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_landlord():
    return {"current_landlord": current_landlord()}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("landlord_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def save_upload(file_storage):
    """Save an uploaded JPG/PNG/WebP under static/uploads and return its URL,
    or None if the file is missing or isn't really one of those image types."""
    if not file_storage or not file_storage.filename or "." not in file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        return None
    head = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    looks_ok = (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG")
                or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))
    if not looks_ok:
        return None
    name = uuid.uuid4().hex[:16] + "." + ("jpg" if ext == "jpeg" else ext)
    file_storage.save(os.path.join(UPLOAD_DIR, name))
    return "/static/uploads/" + name


@app.route("/landlord/register", methods=["GET", "POST"])
def landlord_register():
    if request.method == "GET":
        return render_template("landlord_auth.html", mode="register")

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    errors = []
    if not name:
        errors.append("Your name is required.")
    if not email or "@" not in email:
        errors.append("A valid email is required.")
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if password != confirm:
        errors.append("Passwords don't match.")

    db = get_db()
    if not errors and db.execute(
        "SELECT id FROM landlords WHERE email = ? COLLATE NOCASE", (email,)
    ).fetchone():
        errors.append("An account with that email already exists — try logging in instead.")

    if errors:
        return render_template("landlord_auth.html", mode="register", errors=errors,
                                form=request.form)

    landlord_id = "ll_" + str(uuid.uuid4())[:10]
    db.execute(
        "INSERT INTO landlords (id, name, email, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (landlord_id, name, email, generate_password_hash(password), str(date.today())),
    )
    db.commit()
    session["landlord_id"] = landlord_id
    session["landlord_name"] = name
    session["is_admin"] = False
    return redirect(request.args.get("next") or url_for("dashboard"))


@app.route("/landlord/login", methods=["GET", "POST"])
def landlord_login():
    if request.method == "GET":
        return render_template("landlord_auth.html", mode="login")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    db = get_db()
    row = db.execute(
        "SELECT * FROM landlords WHERE email = ? COLLATE NOCASE", (email,)
    ).fetchone()

    if row is None or not check_password_hash(row["password_hash"], password):
        return render_template(
            "landlord_auth.html", mode="login",
            errors=["Incorrect email or password."], form=request.form,
        )

    session["landlord_id"] = row["id"]
    session["landlord_name"] = row["name"]
    session["is_admin"] = bool(row["is_admin"])
    return redirect(request.args.get("next") or url_for("dashboard"))


@app.route("/landlord/logout", methods=["POST"])
def landlord_logout():
    session.pop("landlord_id", None)
    session.pop("landlord_name", None)
    session.pop("is_admin", None)
    return redirect(url_for("browse"))


# --------------------------------------------------------------------------
# Routes — map browse + JSON api
# --------------------------------------------------------------------------

@app.route("/")
def browse():
    return render_template("map.html", room_types=ROOM_TYPES)


@app.route("/api/rooms")
def api_rooms():
    db = get_db()
    query = "SELECT * FROM rooms WHERE 1=1"
    params = []

    q = request.args.get("q", "").strip()
    rtype = request.args.get("type", "").strip()
    max_price = request.args.get("max_price", "").strip()

    if q:
        query += " AND (title LIKE ? OR city LIKE ? OR address LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if rtype and rtype in ROOM_TYPES:
        query += " AND type = ?"
        params.append(rtype)
    if max_price.isdigit():
        query += " AND price <= ?"
        params.append(int(max_price))

    query += " ORDER BY title"
    rooms = [room_to_dict(r) for r in db.execute(query, params).fetchall()]
    return jsonify(rooms)


@app.route("/room/<room_id>")
def room_detail(room_id):
    db = get_db()
    row = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        return render_template("not_found.html", kind="room"), 404
    room = room_to_dict(row)
    profile = session.get("profile", {})
    return render_template(
        "room.html", room=room, profile=profile, just_posted=request.args.get("posted") == "1",
    )


# --------------------------------------------------------------------------
# Routes — applying
# --------------------------------------------------------------------------

@app.route("/room/<room_id>/apply", methods=["POST"])
def apply_to_room(room_id):
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        return render_template("not_found.html", kind="room"), 404

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    move_in = request.form.get("move_in", "").strip()
    message = request.form.get("message", "").strip()

    errors = []
    if not name:
        errors.append("Full name is required.")
    if not email or "@" not in email:
        errors.append("A valid email is required.")
    if not move_in:
        errors.append("A preferred move-in date is required.")

    if errors:
        profile = {"name": name, "email": email, "phone": phone}
        return render_template(
            "room.html", room=room_to_dict(room), profile=profile, errors=errors,
            form=request.form,
        )

    app_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO applications (id, room_id, applicant_name, email, phone, move_in, "
        "message, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (app_id, room_id, name, email, phone, move_in, message, "Submitted",
         str(date.today())),
    )
    db.commit()

    session["profile"] = {"name": name, "email": email, "phone": phone}
    return redirect(url_for("my_applications", email=email, sent=1))


# --------------------------------------------------------------------------
# Routes — my activity (direct applications + inDrive-style requests/offers)
# --------------------------------------------------------------------------

@app.route("/my-applications")
def my_applications():
    email = request.args.get("email", "").strip() or session.get("profile", {}).get("email", "")
    applications = []
    requests_ = []
    if email:
        db = get_db()
        rows = db.execute(
            "SELECT applications.*, rooms.title AS room_title, rooms.city AS room_city, "
            "rooms.address AS room_address, rooms.lat AS room_lat, rooms.lng AS room_lng "
            "FROM applications JOIN rooms ON rooms.id = applications.room_id "
            "WHERE applications.email = ? COLLATE NOCASE "
            "ORDER BY applications.submitted_at DESC",
            (email,),
        ).fetchall()
        applications = [dict(r) for r in rows]

        req_rows = db.execute(
            "SELECT * FROM requests WHERE email = ? COLLATE NOCASE ORDER BY created_at DESC",
            (email,),
        ).fetchall()
        for rq in req_rows:
            rq = dict(rq)
            offer_rows = db.execute(
                "SELECT offers.*, rooms.title AS room_title, rooms.city AS room_city, "
                "rooms.lat AS room_lat, rooms.lng AS room_lng, rooms.verified AS room_verified "
                "FROM offers JOIN rooms ON rooms.id = offers.room_id "
                "WHERE offers.request_id = ? ORDER BY offers.price ASC",
                (rq["id"],),
            ).fetchall()
            offers = []
            for o in offer_rows:
                o = dict(o)
                o["distance_km"] = round(
                    haversine_km(rq["lat"], rq["lng"], o["room_lat"], o["room_lng"]), 1
                )
                offers.append(o)
            rq["offers"] = offers
            requests_.append(rq)

    return render_template(
        "my_applications.html", email=email, applications=applications, requests=requests_,
        just_sent=request.args.get("sent") == "1",
        just_requested=request.args.get("requested") == "1",
        just_matched=request.args.get("matched") == "1",
    )


@app.route("/directions/<app_id>")
def directions(app_id):
    db = get_db()
    row = db.execute(
        "SELECT applications.*, rooms.title AS room_title, rooms.city AS room_city, "
        "rooms.address AS room_address, rooms.lat AS room_lat, rooms.lng AS room_lng "
        "FROM applications JOIN rooms ON rooms.id = applications.room_id "
        "WHERE applications.id = ?",
        (app_id,),
    ).fetchone()
    if row is None:
        return render_template("not_found.html", kind="application"), 404
    return render_template("directions.html", app=dict(row))


# --------------------------------------------------------------------------
# Routes — renter: request a room, inDrive-style (name your price, pin a spot)
# --------------------------------------------------------------------------

@app.route("/request-a-room", methods=["GET", "POST"])
def request_a_room():
    if request.method == "GET":
        return render_template("request_room.html", room_types=ROOM_TYPES)

    renter_name = request.form.get("renter_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    area_label = request.form.get("area_label", "").strip()
    rtype = request.form.get("type", "").strip()
    budget = request.form.get("budget", "").strip()
    move_in = request.form.get("move_in", "").strip()
    note = request.form.get("note", "").strip()
    lat = request.form.get("lat", "").strip()
    lng = request.form.get("lng", "").strip()

    errors = []
    if not renter_name:
        errors.append("Your name is required.")
    if not email or "@" not in email:
        errors.append("A valid email is required.")
    if rtype not in ROOM_TYPES:
        errors.append("Choose a room type.")
    if not budget.isdigit():
        errors.append("Your budget must be a whole number.")
    if not move_in:
        errors.append("Preferred move-in date is required.")
    try:
        lat_f, lng_f = float(lat), float(lng)
    except ValueError:
        lat_f = lng_f = None
        errors.append("Drop a pin where you'd like to live (or search an area).")

    if errors:
        return render_template("request_room.html", room_types=ROOM_TYPES, errors=errors,
                                form=request.form)

    req_id = "rq_" + str(uuid.uuid4())[:10]
    db = get_db()
    db.execute(
        "INSERT INTO requests (id, renter_name, email, phone, lat, lng, area_label, type, "
        "budget, move_in, note, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (req_id, renter_name, email, phone, lat_f, lng_f, area_label, rtype, int(budget),
         move_in, note, "Open", str(date.today())),
    )
    db.commit()
    session["profile"] = {"name": renter_name, "email": email, "phone": phone}

    # Live-push the new request to every landlord currently on "Nearby requests" —
    # same as a new ride request popping up on a driver's map in real time.
    socketio.emit("new_request", {
        "id": req_id, "renter_name": renter_name, "lat": lat_f, "lng": lng_f,
        "area_label": area_label, "type": rtype, "budget": int(budget),
        "move_in": move_in, "note": note,
    }, room="landlords")

    return redirect(url_for("my_applications", email=email, requested=1))


@app.route("/api/requests/<request_id>/offers")
def api_request_offers(request_id):
    db = get_db()
    rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if rq is None:
        return jsonify({"error": "not found"}), 404
    rq = dict(rq)
    offer_rows = db.execute(
        "SELECT offers.*, rooms.title AS room_title, rooms.city AS room_city, "
        "rooms.lat AS room_lat, rooms.lng AS room_lng, rooms.verified AS room_verified "
        "FROM offers JOIN rooms ON rooms.id = offers.room_id "
        "WHERE offers.request_id = ? ORDER BY offers.price ASC",
        (request_id,),
    ).fetchall()
    offers = []
    for o in offer_rows:
        o = dict(o)
        o["distance_km"] = round(haversine_km(rq["lat"], rq["lng"], o["room_lat"], o["room_lng"]), 1)
        offers.append(o)
    return jsonify({"request_status": rq["status"], "offers": offers})


@app.route("/offers/<offer_id>/accept", methods=["POST"])
def accept_offer(offer_id):
    db = get_db()
    offer = db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if offer is None:
        return render_template("not_found.html", kind="offer"), 404
    rq = db.execute("SELECT * FROM requests WHERE id = ?", (offer["request_id"],)).fetchone()

    db.execute("UPDATE offers SET status = 'Accepted' WHERE id = ?", (offer_id,))
    db.execute(
        "UPDATE offers SET status = 'Declined' WHERE request_id = ? AND id != ?",
        (offer["request_id"], offer_id),
    )
    db.execute("UPDATE requests SET status = 'Matched' WHERE id = ?", (offer["request_id"],))

    app_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO applications (id, room_id, applicant_name, email, phone, move_in, "
        "message, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (app_id, offer["room_id"], rq["renter_name"], rq["email"], rq["phone"], rq["move_in"],
         offer["message"], "Approved", str(date.today())),
    )
    db.commit()

    # Tell every landlord's "Nearby requests" map this request is gone, and tell
    # anyone watching the request's own offer list that it's been matched —
    # same as a ride request disappearing from other drivers once accepted.
    socketio.emit("request_matched", {"request_id": offer["request_id"]}, room="landlords")
    socketio.emit("request_matched", {"request_id": offer["request_id"]},
                   room=f"request_{offer['request_id']}")

    return redirect(url_for("my_applications", email=rq["email"], matched=1))


# --------------------------------------------------------------------------
# Routes — landlord: nearby requests + making an offer (inDrive-style)
# --------------------------------------------------------------------------

@app.route("/nearby-requests")
@login_required
def nearby_requests():
    return render_template("nearby_requests.html")


@app.route("/api/requests")
def api_requests():
    db = get_db()
    rows = db.execute("SELECT * FROM requests WHERE status = 'Open' ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@socketio.on("join_landlords")
def on_join_landlords():
    join_room("landlords")


@socketio.on("join_request")
def on_join_request(data):
    request_id = (data or {}).get("request_id")
    if request_id:
        join_room(f"request_{request_id}")


@app.route("/nearby-requests/<request_id>/offer", methods=["GET", "POST"])
@login_required
def make_offer(request_id):
    db = get_db()
    rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if rq is None:
        return render_template("not_found.html", kind="request"), 404
    rq = dict(rq)

    landlord = current_landlord()
    room_rows = db.execute(
        "SELECT * FROM rooms WHERE landlord_id = ? ORDER BY title", (landlord["id"],)
    ).fetchall()
    rooms_with_distance = []
    for r in room_rows:
        r = dict(r)
        r["distance_km"] = round(haversine_km(rq["lat"], rq["lng"], r["lat"], r["lng"]), 1)
        rooms_with_distance.append(r)
    rooms_with_distance.sort(key=lambda r: r["distance_km"])

    if request.method == "GET":
        return render_template("make_offer.html", req=rq, rooms=rooms_with_distance)

    room_id = request.form.get("room_id", "")
    price = request.form.get("price", "").strip()
    message = request.form.get("message", "").strip()

    errors = []
    chosen_room = next((r for r in rooms_with_distance if r["id"] == room_id), None)
    if not chosen_room:
        errors.append("Choose which of your listings this offer is for.")
    if not price.isdigit():
        errors.append("Price must be a whole number.")

    if errors:
        return render_template("make_offer.html", req=rq, rooms=rooms_with_distance,
                                errors=errors, form=request.form)

    offer_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO offers (id, request_id, room_id, landlord_name, landlord_id, price, "
        "message, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (offer_id, request_id, room_id, landlord["name"], landlord["id"], int(price), message,
         "Pending", str(date.today())),
    )
    db.commit()

    # Live-push the new offer to the renter's "My activity" page, if they're
    # watching it — same as a driver's quote appearing instantly for a rider.
    socketio.emit("new_offer", {
        "id": offer_id, "request_id": request_id, "room_id": room_id,
        "room_title": chosen_room["title"], "room_city": chosen_room["city"],
        "room_verified": chosen_room["verified"], "distance_km": chosen_room["distance_km"],
        "landlord_name": landlord["name"], "price": int(price), "message": message,
        "status": "Pending",
    }, room=f"request_{request_id}")

    return redirect(url_for("nearby_requests", sent=1))


# --------------------------------------------------------------------------
# Routes — landlord: list a room
# --------------------------------------------------------------------------

@app.route("/list-a-room", methods=["GET", "POST"])
@login_required
def list_a_room():
    if request.method == "GET":
        return render_template("list_room.html", room_types=ROOM_TYPES,
                                safety_options=SAFETY_OPTIONS)

    title = request.form.get("title", "").strip()
    city = request.form.get("city", "").strip()
    address = request.form.get("address", "").strip()
    rtype = request.form.get("type", "").strip()
    price = request.form.get("price", "").strip()
    size_sqft = request.form.get("size_sqft", "").strip()
    available = request.form.get("available", "").strip()
    description = request.form.get("description", "").strip()
    photo_url = request.form.get("photo_url", "").strip()
    lat = request.form.get("lat", "").strip()
    lng = request.form.get("lng", "").strip()
    safety = request.form.getlist("safety")
    landlord = current_landlord()

    errors = []
    if not title:
        errors.append("Give the listing a title.")
    if not city:
        errors.append("City is required.")
    if not address:
        errors.append("Street address is required.")
    if rtype not in ROOM_TYPES:
        errors.append("Choose a room type.")
    if not price.isdigit():
        errors.append("Monthly price must be a whole number.")
    if not size_sqft.isdigit():
        errors.append("Size in square feet must be a whole number.")
    if not available:
        errors.append("Say when the room is available.")
    if not description:
        errors.append("Add a short description.")
    try:
        lat_f, lng_f = float(lat), float(lng)
    except ValueError:
        lat_f = lng_f = None
        errors.append("Drop a pin on the map, search an address, or type coordinates "
                       "manually to set the location.")

    if errors:
        return render_template(
            "list_room.html", room_types=ROOM_TYPES, safety_options=SAFETY_OPTIONS,
            errors=errors, form=request.form,
        )

    room_id = "rm_" + str(uuid.uuid4())[:10]
    db = get_db()
    db.execute(
        "INSERT INTO rooms (id, title, city, address, type, price, size_sqft, available, "
        "description, safety, verified, lat, lng, photo_url, landlord_name, landlord_id, "
        "posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (room_id, title, city, address, rtype, int(price), int(size_sqft), available,
         description, json.dumps(safety), 0, lat_f, lng_f, photo_url or None,
         landlord["name"], landlord["id"], str(date.today())),
    )
    db.commit()
    return redirect(url_for("room_detail", room_id=room_id, posted=1))


# --------------------------------------------------------------------------
# Routes — landlord dashboard
# --------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    landlord = current_landlord()
    db = get_db()
    rows = db.execute(
        "SELECT applications.*, rooms.title AS room_title, rooms.city AS room_city "
        "FROM applications JOIN rooms ON rooms.id = applications.room_id "
        "WHERE rooms.landlord_id = ? "
        "ORDER BY rooms.title, applications.submitted_at DESC",
        (landlord["id"],),
    ).fetchall()
    applications = [dict(r) for r in rows]

    room_rows = db.execute(
        "SELECT id, title, city, verified FROM rooms WHERE landlord_id = ? ORDER BY title",
        (landlord["id"],),
    ).fetchall()
    room_info = {r["id"]: dict(r) for r in room_rows}

    grouped = []
    seen = {}
    for a in applications:
        key = a["room_id"]
        if key not in seen:
            seen[key] = {
                "room_id": key,
                "room_title": a["room_title"],
                "room_city": a["room_city"],
                "verified": room_info.get(key, {}).get("verified", 0),
                "apps": [],
            }
            grouped.append(seen[key])
        seen[key]["apps"].append(a)

    # Listings with no applications yet still belong on the dashboard so a
    # landlord can see (and verify) everything they've posted.
    listed_room_ids = {g["room_id"] for g in grouped}
    for room_id, info in room_info.items():
        if room_id not in listed_room_ids:
            grouped.append({
                "room_id": room_id, "room_title": info["title"], "room_city": info["city"],
                "verified": info["verified"], "apps": [],
            })

    return render_template("dashboard.html", grouped=grouped, statuses=STATUSES,
                            landlord=landlord)


@app.route("/application/<app_id>/status", methods=["POST"])
@login_required
def update_status(app_id):
    new_status = request.form.get("status", "")
    landlord = current_landlord()
    db = get_db()
    owns = db.execute(
        "SELECT applications.id FROM applications JOIN rooms ON rooms.id = applications.room_id "
        "WHERE applications.id = ? AND rooms.landlord_id = ?",
        (app_id, landlord["id"]),
    ).fetchone()
    if owns and new_status in STATUSES:
        db.execute("UPDATE applications SET status = ? WHERE id = ?", (new_status, app_id))
        db.commit()
    return redirect(url_for("dashboard"))


@app.route("/room/<room_id>/toggle-verified", methods=["POST"])
@login_required
def toggle_verified(room_id):
    landlord = current_landlord()
    db = get_db()
    row = db.execute(
        "SELECT verified FROM rooms WHERE id = ? AND landlord_id = ?",
        (room_id, landlord["id"]),
    ).fetchone()
    if row is not None:
        db.execute("UPDATE rooms SET verified = ? WHERE id = ?", (0 if row["verified"] else 1, room_id))
        db.commit()
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Restaurants + admin (photo uploads)
# --------------------------------------------------------------------------

@app.route("/api/places")
def api_places():
    db = get_db()
    q = request.args.get("q", "").strip()
    query, params = "SELECT * FROM places", []
    if q:
        query += " WHERE name LIKE ? OR cuisine LIKE ? OR address LIKE ? OR description LIKE ?"
        params = [f"%{q}%"] * 4
    return jsonify([dict(r) for r in db.execute(query + " ORDER BY name", params).fetchall()])


@app.errorhandler(413)
def too_large(e):
    if session.get("is_admin"):
        return redirect(url_for("admin", msg="That image is too large (6 MB max)."))
    return "That file is too large.", 413


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    rooms = [room_to_dict(r) for r in db.execute(
        "SELECT * FROM rooms ORDER BY type, title").fetchall()]
    places = [dict(r) for r in db.execute("SELECT * FROM places ORDER BY name").fetchall()]
    return render_template("admin.html", rooms=rooms, places=places,
                           msg=request.args.get("msg"))


def _admin_photo(table, item_id):
    url = save_upload(request.files.get("photo"))
    if not url:
        return redirect(url_for("admin", msg="That file wasn't a valid JPG, PNG, or WebP image."))
    db = get_db()
    db.execute(f"UPDATE {table} SET photo_url = ? WHERE id = ?", (url, item_id))
    db.commit()
    return redirect(url_for("admin", msg="Photo saved."))


@app.route("/admin/room/<room_id>/photo", methods=["POST"])
@admin_required
def admin_room_photo(room_id):
    return _admin_photo("rooms", room_id)


@app.route("/admin/place/<place_id>/photo", methods=["POST"])
@admin_required
def admin_place_photo(place_id):
    return _admin_photo("places", place_id)


@app.route("/admin/place/add", methods=["POST"])
@admin_required
def admin_place_add():
    f = request.form
    name, address = f.get("name", "").strip(), f.get("address", "").strip()
    try:
        lat, lng = float(f.get("lat", "")), float(f.get("lng", ""))
    except ValueError:
        lat = lng = None
    if not name or not address or lat is None:
        return redirect(url_for("admin", msg="A restaurant needs a name, address, and coordinates."))
    photo = save_upload(request.files.get("photo"))
    db = get_db()
    db.execute(
        "INSERT INTO places (id, name, owner_name, cuisine, address, price_range, description, "
        "lat, lng, photo_url, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("pl_" + uuid.uuid4().hex[:10], name, f.get("owner_name", "").strip(),
         f.get("cuisine", "").strip(), address, f.get("price_range", "").strip(),
         f.get("description", "").strip(), lat, lng, photo, str(date.today())),
    )
    db.commit()
    return redirect(url_for("admin", msg="Restaurant added."))


if __name__ == "__main__":
    init_db()
    socketio.run(app, debug=True)
else:
    init_db()
