import os
import re
import time
import asyncio
import sqlite3
import logging
import urllib.request
import secrets
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, HTTPException, Query, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
import yt_dlp
from ytmusicapi import YTMusic


# =========================================================
# LOAD ENVIRONMENT VARIABLES
# =========================================================

load_dotenv()


# =========================================================
# CONFIGURATION
# =========================================================

DOWNLOAD_DIR = os.getenv(
    "DOWNLOAD_DIR",
    "downloads"
)

CACHE_EXPIRE_HOURS = float(
    os.getenv(
        "CACHE_EXPIRE_HOURS",
        "24"
    )
)

MAX_VIDEO_QUALITY = os.getenv(
    "MAX_VIDEO_QUALITY",
    "720"
)

PORT = int(
    os.getenv(
        "PORT",
        "8000"
    )
)

COOKIE_URL = os.getenv(
    "COOKIE_URL",
    ""
)

# YouTube player clients. Avoid the deprecated/problematic tv_downgraded
# client that can cause "The page needs to be reloaded" errors.
YOUTUBE_PLAYER_CLIENTS = os.getenv(
    "YOUTUBE_PLAYER_CLIENTS",
    "default,web_embedded"
).strip()

COOKIES_FILE = "cookies.txt"

DB_FILE = "cache.db"

SESSION_COOKIE = "vba_session"
WALLET_CURRENCY = "INR"
PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()


# =========================================================
# API KEY AUTHENTICATION
# =========================================================
# Set API_KEY in Heroku Config Vars. Keep this value secret.
# Client requests should send: X-API-Key: <your-key>
# Authorization: Bearer <your-key> is also accepted.
# For compatibility, ?api_key=<your-key> is also accepted.

API_KEY = os.getenv("API_KEY", "").strip()


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None, description="API key (legacy/query compatibility)")
):
    """Protect API endpoints with a server-side API key."""

    if not API_KEY:
        logger.error("API_KEY is not configured on the server.")
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured on the server."
        )

    # Prefer the HTTP header. Also accept ?api_key=... for compatibility
    # with existing Music Bot clients.
    supplied_key = (x_api_key or api_key or "").strip()

    # Also accept Authorization: Bearer <key> for clients that prefer it.
    if not supplied_key and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied_key = token.strip()

    if supplied_key and API_KEY and hmac.compare_digest(supplied_key, API_KEY):
        return True

    # User-generated Velocity API keys.
    if supplied_key.startswith("vba_"):
        now = time.time()
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT u.id AS user_id, p.requests, s.requests_used, s.expires FROM user_keys k JOIN subscriptions s ON s.user_id=k.user_id AND s.status='active' AND s.expires>? JOIN plans p ON p.id=s.plan_id AND p.active=1 JOIN users u ON u.id=k.user_id WHERE k.key=? AND k.revoked=0 ORDER BY s.expires DESC LIMIT 1", (now, supplied_key)).fetchone()
        if row:
            if row[2] >= row[1]:
                raise HTTPException(status_code=429, detail="API request limit reached for your current subscription.")
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("UPDATE subscriptions SET requests_used=requests_used+1 WHERE user_id=? AND status='active' AND expires>?", (row[0], now))
                conn.commit()
            return True

    raise HTTPException(status_code=401, detail="Invalid, revoked, or inactive API key.")


# =========================================================
# DOWNLOAD PERFORMANCE SETTINGS
# =========================================================

CONCURRENT_FRAGMENT_DOWNLOADS = int(
    os.getenv(
        "CONCURRENT_FRAGMENT_DOWNLOADS",
        "15"
    )
)

HTTP_CHUNK_SIZE = int(
    os.getenv(
        "HTTP_CHUNK_SIZE",
        "10485760"
    )
)

SOCKET_TIMEOUT = int(
    os.getenv(
        "SOCKET_TIMEOUT",
        "15"
    )
)

RETRIES = int(
    os.getenv(
        "RETRIES",
        "5"
    )
)

FRAGMENT_RETRIES = int(
    os.getenv(
        "FRAGMENT_RETRIES",
        "5"
    )
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


# =========================================================
# DOWNLOAD DIRECTORY
# =========================================================

os.makedirs(
    DOWNLOAD_DIR,
    exist_ok=True
)


# =========================================================
# DATABASE & CACHE SYSTEM
# =========================================================

def init_db():

    """Initializes the SQLite database for caching metadata safely."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT,
                    title TEXT,
                    file_name TEXT,
                    file_path TEXT,
                    file_type TEXT,
                    file_size INTEGER,
                    duration INTEGER,
                    created_time REAL,
                    thumbnail TEXT,
                    UNIQUE(video_id, file_type)
                )
                '''
            )

            conn.execute("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT, password_hash TEXT NOT NULL, wallet REAL NOT NULL DEFAULT 0, created_time REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, price REAL NOT NULL, requests INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, started REAL NOT NULL, expires REAL NOT NULL, requests_used INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS user_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, key TEXT UNIQUE NOT NULL, created_time REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS wallet_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, reference TEXT, created_time REAL NOT NULL)""")
            defaults=[("Free",0,100), ("Basic",99,5000), ("Pro",199,25000), ("Premium",499,100000)]
            for name,price,requests in defaults:
                conn.execute("INSERT OR IGNORE INTO plans(name,price,requests) VALUES(?,?,?)",(name,price,requests))
            conn.commit()

        logger.info("SQLite database initialized.")

    except Exception as e:

        logger.error(
            f"Database initialization failed: {e}"
        )


def get_cached_metadata(
    video_id: str,
    file_type: str
) -> Optional[Dict[str, Any]]:

    """Retrieves cached metadata from SQLite and verifies file existence."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.row_factory = sqlite3.Row

            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM downloads
                WHERE video_id = ?
                AND file_type = ?
                """,
                (
                    video_id,
                    file_type
                )
            )

            row = cur.fetchone()

            if row:

                if (
                    os.path.isfile(
                        row["file_path"]
                    )
                    and
                    os.path.getsize(
                        row["file_path"]
                    ) > 0
                ):

                    return dict(row)

                else:

                    logger.warning(
                        f"File {row['file_name']} "
                        "missing from disk. "
                        "Removing DB entry."
                    )

                    cur.execute(
                        """
                        DELETE FROM downloads
                        WHERE id = ?
                        """,
                        (
                            row["id"],
                        )
                    )

                    conn.commit()

            return None

    except Exception as e:

        logger.error(
            f"Error accessing cache DB: {e}"
        )

        return None


def save_cached_metadata(
    data: Dict[str, Any],
    file_type: str
):

    """Saves download metadata to SQLite."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.execute(
                '''
                INSERT OR REPLACE INTO downloads
                (
                    video_id,
                    title,
                    file_name,
                    file_path,
                    file_type,
                    file_size,
                    duration,
                    created_time,
                    thumbnail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    data["videoId"],
                    data["title"],
                    data["filename"],
                    data["path"],
                    file_type,
                    data["filesize"],
                    data["duration"],
                    time.time(),
                    data["thumbnail"]
                )
            )

            conn.commit()

    except Exception as e:

        logger.error(
            f"Error saving to cache DB: {e}"
        )


def find_legacy_cached_file(
    video_id: str,
    ext: str
) -> Optional[str]:

    """Fallback to check un-indexed files downloaded before SQLite was added."""

    if not video_id:

        return None

    suffix = f"_{video_id}.{ext}"

    try:

        with os.scandir(
            DOWNLOAD_DIR
        ) as entries:

            for entry in entries:

                if entry.name.endswith(
                    suffix
                ):

                    return entry.name

    except Exception as e:

        logger.error(
            f"Error reading {DOWNLOAD_DIR}: {e}"
        )

    return None


# =========================================================
# CACHE CLEANUP
# =========================================================

async def cache_cleanup_task():

    """Background task to delete old files and clean up database."""

    while True:

        try:

            logger.info(
                "Running advanced cache cleanup..."
            )

            expiry_time = (
                time.time()
                -
                (
                    CACHE_EXPIRE_HOURS
                    * 3600
                )
            )

            def perform_cleanup():

                deleted_files = 0
                db_cleaned = 0

                with sqlite3.connect(
                    DB_FILE,
                    timeout=15.0
                ) as conn:

                    conn.row_factory = sqlite3.Row

                    cur = conn.cursor()

                    # -----------------------------------------
                    # 1. Scan disk for expired files
                    # -----------------------------------------

                    if os.path.exists(
                        DOWNLOAD_DIR
                    ):

                        for entry in os.scandir(
                            DOWNLOAD_DIR
                        ):

                            if entry.is_file():

                                file_stat = entry.stat()

                                if (
                                    file_stat.st_mtime
                                    <
                                    expiry_time
                                ):

                                    try:

                                        os.remove(
                                            entry.path
                                        )

                                        deleted_files += 1

                                        cur.execute(
                                            """
                                            DELETE FROM downloads
                                            WHERE file_name = ?
                                            """,
                                            (
                                                entry.name,
                                            )
                                        )

                                    except Exception as e:

                                        logger.warning(
                                            f"Could not delete old "
                                            f"file {entry.name}: {e}"
                                        )

                    # -----------------------------------------
                    # 2. Remove phantom DB records
                    # -----------------------------------------

                    cur.execute(
                        """
                        SELECT id, file_path
                        FROM downloads
                        """
                    )

                    all_records = cur.fetchall()

                    for record in all_records:

                        if not os.path.exists(
                            record["file_path"]
                        ):

                            cur.execute(
                                """
                                DELETE FROM downloads
                                WHERE id = ?
                                """,
                                (
                                    record["id"],
                                )
                            )

                            db_cleaned += 1

                    conn.commit()

                return (
                    deleted_files,
                    db_cleaned
                )

            deleted_files, db_cleaned = (
                await asyncio.to_thread(
                    perform_cleanup
                )
            )

            if (
                deleted_files > 0
                or
                db_cleaned > 0
            ):

                logger.info(
                    f"Cleanup complete: "
                    f"Deleted {deleted_files} "
                    f"old files on disk, "
                    f"cleared {db_cleaned} "
                    f"orphaned DB records."
                )

            else:

                logger.info(
                    "Cleanup complete: "
                    "No expired files found."
                )

        except Exception as e:

            logger.error(
                "Cache cleanup encountered an error "
                f"(will retry next cycle): {e}"
            )

        await asyncio.sleep(
            3600
        )


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info(
        "Starting MAGMA Music API..."
    )

    init_db()

    # -----------------------------------------
    # Download cookies
    # -----------------------------------------

    if COOKIE_URL:

        try:

            urllib.request.urlretrieve(
                COOKIE_URL,
                COOKIES_FILE
            )

            logger.info(
                "Successfully downloaded "
                "cookies.txt from COOKIE_URL"
            )

        except Exception as e:

            logger.error(
                f"Failed to download cookies "
                f"from COOKIE_URL: {e}"
            )

    # -----------------------------------------
    # Start cleanup worker
    # -----------------------------------------

    cleanup_worker = asyncio.create_task(
        cache_cleanup_task()
    )

    yield

    # -----------------------------------------
    # Shutdown
    # -----------------------------------------

    logger.info(
        "Shutting down MAGMA Music API..."
    )

    cleanup_worker.cancel()


# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(
    title="YouTube Downloader & Search API",
    version="2.3.0-Production",
    lifespan=lifespan
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# =========================================================
# MAGMA.HTML DEVELOPER PORTAL
# =========================================================

HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal.html")

try:

    with open(
        HTML_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        DEVELOPER_PORTAL_HTML = f.read()

    logger.info(
        "portal.html loaded successfully."
    )

except Exception as e:

    logger.error(
        f"Failed to load Magma.html: {e}"
    )

    DEVELOPER_PORTAL_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>MAGMA API</title>
    </head>
    <body>
        <h1>MAGMA API</h1>
        <p>
            Developer portal could not be loaded.
        </p>
    </body>
    </html>
    """


# =========================================================
# YOUTUBE MUSIC
# =========================================================

ytmusic = YTMusic()


# =========================================================
# VIDEO ID EXTRACTION
# =========================================================

def extract_video_id(
    url: str
) -> Optional[str]:

    """Extracts the 11-character YouTube Video ID."""

    if not url:

        return None

    if re.match(
        r"^[0-9A-Za-z_-]{11}$",
        url
    ):

        return url

    pattern = (
        r"(?:youtu\.be\/|v=|\/shorts\/|"
        r"\/embed\/|\/v\/)"
        r"([0-9A-Za-z_-]{11})"
    )

    match = re.search(
        pattern,
        url
    )

    if match:

        return match.group(1)

    match = re.search(
        r"[0-9A-Za-z_-]{11}",
        url
    )

    return (
        match.group(0)
        if match
        else None
    )


# =========================================================
# BASE YT-DLP OPTIONS
# =========================================================

def get_base_ydl_opts() -> Dict[str, Any]:

    opts = {

        "outtmpl":
            f"{DOWNLOAD_DIR}/%(title).150s_%(id)s.%(ext)s",

        "restrictfilenames":
            True,

        "noplaylist":
            True,

        "quiet":
            False,

        "no_warnings":
            False,

        "retries":
            RETRIES,

        "fragment_retries":
            FRAGMENT_RETRIES,

        "socket_timeout":
            SOCKET_TIMEOUT,

        "continuedl":
            True,

        "js_runtimes":
            {
                "node": {}
            },

        "remote_components":
            [
                "ejs:github"
            ]
    }

    if os.path.exists(
        COOKIES_FILE
    ):

        opts["cookiefile"] = (
            COOKIES_FILE
        )

        logger.info(
            f"Loaded cookies from "
            f"{COOKIES_FILE}"
        )

    return opts


# =========================================================
# THUMBNAIL
# =========================================================

def fetch_thumbnail_sync(
    url: str
) -> Dict[str, Any]:

    opts = get_base_ydl_opts()

    opts["skip_download"] = True

    try:

        with yt_dlp.YoutubeDL(
            opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

            return {

                "title":
                    info.get("title"),

                "thumbnail":
                    info.get("thumbnail"),

                "videoId":
                    info.get("id")
            }

    except Exception as e:

        logger.error(
            f"Thumbnail fetch error: {e}"
        )

        raise RuntimeError(
            f"Failed to fetch thumbnail: {str(e)}"
        )


# =========================================================
# AUDIO DOWNLOAD
# =========================================================

def download_audio_sync(
    url: str
) -> Dict[str, Any]:

    video_id = extract_video_id(
        url
    )

    # -----------------------------------------
    # DATABASE CACHE
    # -----------------------------------------

    if video_id:

        cached_data = get_cached_metadata(
            video_id,
            "mp3"
        )

        if cached_data:

            logger.info(
                f"Database cache hit! "
                f"Returning audio for {video_id}"
            )

            return {

                "status":
                    True,

                "title":
                    cached_data["title"],

                "duration":
                    cached_data["duration"],

                "thumbnail":
                    cached_data["thumbnail"],

                "filename":
                    cached_data["file_name"],

                "path":
                    cached_data["file_path"],

                "download_url":
                    f"/files/"
                    f"{cached_data['file_name']}",

                "videoId":
                    video_id,

                "uploader":
                    "Cached",

                "filesize":
                    cached_data["file_size"]
            }

        # -----------------------------------------
        # LEGACY CACHE
        # -----------------------------------------

        legacy_file = find_legacy_cached_file(
            video_id,
            "mp3"
        )

        if legacy_file:

            path = os.path.join(
                DOWNLOAD_DIR,
                legacy_file
            )

            if (
                os.path.isfile(path)
                and
                os.path.getsize(path) > 0
            ):

                logger.info(
                    f"Legacy disk cache hit "
                    f"for {video_id}. "
                    "Saving to DB."
                )

                data = {

                    "videoId":
                        video_id,

                    "title":
                        legacy_file[
                            :-
                            len(
                                f"_{video_id}.mp3"
                            )
                        ],

                    "filename":
                        legacy_file,

                    "path":
                        path,

                    "type":
                        "mp3",

                    "filesize":
                        os.path.getsize(path),

                    "duration":
                        0,

                    "thumbnail":
                        f"https://i.ytimg.com/vi/"
                        f"{video_id}/hqdefault.jpg"
                }

                save_cached_metadata(
                    data,
                    "mp3"
                )

                data["status"] = True

                data["download_url"] = (
                    f"/files/{legacy_file}"
                )

                data["uploader"] = "Cached"

                return data

    # -----------------------------------------
    # ACTUAL DOWNLOAD
    # -----------------------------------------

    logger.info(
        f"Starting audio download for: {url}"
    )

    opts = get_base_ydl_opts()

    opts.update({

        "format":
            "140/ba[ext=m4a]/bestaudio/best",

        "writethumbnail":
            False,

        "postprocessors": [

            {

                "key":
                    "FFmpegExtractAudio",

                "preferredcodec":
                    "mp3",

                "preferredquality":
                    "192"
            }
        ],

        "extractor_args": {

            "youtube": [
                f"player_client={YOUTUBE_PLAYER_CLIENTS}"
            ]
        },

        # -----------------------------------------
        # ENV CONFIGURABLE SPEED SETTINGS
        # -----------------------------------------

        "concurrent_fragment_downloads":
            CONCURRENT_FRAGMENT_DOWNLOADS,

        "http_chunk_size":
            HTTP_CHUNK_SIZE,

        "nocheckcertificate":
            True,

        "noprogress":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "updatetime":
            False,

        "clean_infojson":
            False,

        "retries":
            RETRIES,

        "fragment_retries":
            FRAGMENT_RETRIES,

        "socket_timeout":
            SOCKET_TIMEOUT,

        "postprocessor_args": [

            "-threads",
            "0",

            "-vn",
            "-sn"
        ]
    })

    try:

        with yt_dlp.YoutubeDL(
            opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            filename = ydl.prepare_filename(
                info
            )

            base_path, _ = os.path.splitext(
                filename
            )

            final_path = (
                f"{base_path}.mp3"
            )

            if (
                not os.path.isfile(
                    final_path
                )
                or
                os.path.getsize(
                    final_path
                ) == 0
            ):

                raise RuntimeError(
                    "Downloaded file is missing "
                    "or empty."
                )

            logger.info(
                f"Successfully downloaded audio: "
                f"{final_path}"
            )

            response_data = {

                "status":
                    True,

                "title":
                    info.get(
                        "title",
                        ""
                    ),

                "duration":
                    info.get(
                        "duration",
                        0
                    ),

                "thumbnail":
                    info.get(
                        "thumbnail",
                        ""
                    ),

                "filename":
                    os.path.basename(
                        final_path
                    ),

                "path":
                    final_path,

                "download_url":
                    f"/files/"
                    f"{os.path.basename(final_path)}",

                "videoId":
                    info.get("id"),

                "uploader":
                    info.get("uploader"),

                "filesize":
                    os.path.getsize(
                        final_path
                    )
            }

            save_cached_metadata(
                response_data,
                "mp3"
            )

            return response_data

    except yt_dlp.utils.DownloadError as e:

        logger.error(
            f"yt-dlp error downloading audio "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Download Error: {str(e)}"
        )

    except Exception as e:

        logger.error(
            f"Unexpected error downloading audio "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Internal Server Error: {str(e)}"
        )


# =========================================================
# VIDEO DOWNLOAD
# =========================================================

def download_video_sync(
    url: str
) -> Dict[str, Any]:

    video_id = extract_video_id(
        url
    )

    # -----------------------------------------
    # DATABASE CACHE
    # -----------------------------------------

    if video_id:

        cached_data = get_cached_metadata(
            video_id,
            "mp4"
        )

        if cached_data:

            logger.info(
                f"Database cache hit! "
                f"Returning video for {video_id}"
            )

            return {

                "status":
                    True,

                "title":
                    cached_data["title"],

                "thumbnail":
                    cached_data["thumbnail"],

                "filename":
                    cached_data["file_name"],

                "path":
                    cached_data["file_path"],

                "download_url":
                    f"/files/"
                    f"{cached_data['file_name']}",

                "duration":
                    cached_data["duration"],

                "videoId":
                    video_id,

                "uploader":
                    "Cached",

                "filesize":
                    cached_data["file_size"]
            }

        # -----------------------------------------
        # LEGACY CACHE
        # -----------------------------------------

        legacy_file = find_legacy_cached_file(
            video_id,
            "mp4"
        )

        if legacy_file:

            path = os.path.join(
                DOWNLOAD_DIR,
                legacy_file
            )

            if (
                os.path.isfile(path)
                and
                os.path.getsize(path) > 0
            ):

                logger.info(
                    f"Legacy disk cache hit "
                    f"for {video_id}. "
                    "Saving to DB."
                )

                data = {

                    "videoId":
                        video_id,

                    "title":
                        legacy_file[
                            :-
                            len(
                                f"_{video_id}.mp4"
                            )
                        ],

                    "filename":
                        legacy_file,

                    "path":
                        path,

                    "type":
                        "mp4",

                    "filesize":
                        os.path.getsize(path),

                    "duration":
                        0,

                    "thumbnail":
                        f"https://i.ytimg.com/vi/"
                        f"{video_id}/hqdefault.jpg"
                }

                save_cached_metadata(
                    data,
                    "mp4"
                )

                data["status"] = True

                data["download_url"] = (
                    f"/files/{legacy_file}"
                )

                data["uploader"] = "Cached"

                return data

    # -----------------------------------------
    # ACTUAL DOWNLOAD
    # -----------------------------------------

    logger.info(
        f"Starting video download for: {url}"
    )

    opts = get_base_ydl_opts()

    opts.update({

        "format":
            f"bv*[height<={MAX_VIDEO_QUALITY}]"
            f"[ext=mp4]+ba[ext=m4a]/"
            f"b[height<={MAX_VIDEO_QUALITY}]"
            f"[ext=mp4]/best",

        "merge_output_format":
            "mp4",

        "writethumbnail":
            False,

        "embedthumbnail":
            False,

        "extractor_args": {

            "youtube": [
                f"player_client={YOUTUBE_PLAYER_CLIENTS}"
            ]
        },

        # -----------------------------------------
        # ENV CONFIGURABLE SPEED SETTINGS
        # -----------------------------------------

        "concurrent_fragment_downloads":
            CONCURRENT_FRAGMENT_DOWNLOADS,

        "http_chunk_size":
            HTTP_CHUNK_SIZE,

        "nocheckcertificate":
            True,

        "noprogress":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "updatetime":
            False,

        "clean_infojson":
            False,

        "retries":
            RETRIES,

        "fragment_retries":
            FRAGMENT_RETRIES,

        "socket_timeout":
            SOCKET_TIMEOUT,

        "postprocessor_args": [

            "-threads",
            "0"
        ]
    })

    try:

        with yt_dlp.YoutubeDL(
            opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            filename = ydl.prepare_filename(
                info
            )

            base_path, _ = os.path.splitext(
                filename
            )

            final_path = (
                f"{base_path}.mp4"
            )

            # -----------------------------------------
            # Check possible output extensions
            # -----------------------------------------

            for ext in [
                ".mp4",
                ".webm",
                ".mkv"
            ]:

                test_path = (
                    f"{base_path}{ext}"
                )

                if (
                    os.path.isfile(
                        test_path
                    )
                    and
                    os.path.getsize(
                        test_path
                    ) > 0
                ):

                    final_path = test_path

                    break

            if not (
                os.path.isfile(
                    final_path
                )
                and
                os.path.getsize(
                    final_path
                ) > 0
            ):

                raise RuntimeError(
                    "Downloaded file not found "
                    "or is empty."
                )

            logger.info(
                f"Successfully downloaded video: "
                f"{final_path}"
            )

            response_data = {

                "status":
                    True,

                "title":
                    info.get(
                        "title",
                        ""
                    ),

                "thumbnail":
                    info.get(
                        "thumbnail",
                        ""
                    ),

                "filename":
                    os.path.basename(
                        final_path
                    ),

                "path":
                    final_path,

                "download_url":
                    f"/files/"
                    f"{os.path.basename(final_path)}",

                "duration":
                    info.get(
                        "duration",
                        0
                    ),

                "videoId":
                    info.get("id"),

                "uploader":
                    info.get("uploader"),

                "filesize":
                    os.path.getsize(
                        final_path
                    )
            }

            save_cached_metadata(
                response_data,
                "mp4"
            )

            return response_data

    except yt_dlp.utils.DownloadError as e:

        logger.error(
            f"yt-dlp error downloading video "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Download Error: {str(e)}"
        )

    except Exception as e:

        logger.error(
            f"Unexpected error downloading video "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Internal Server Error: {str(e)}"
        )


# =========================================================
# ROOT — DEVELOPER PORTAL
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def root():

    return HTMLResponse(
        content=DEVELOPER_PORTAL_HTML,
        status_code=200
    )


# =========================================================
# VELOCITY BOTS PLATFORM — ACCOUNT / WALLET / SUBSCRIPTIONS
# =========================================================

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return salt.hex() + ":" + digest.hex()


def _check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 180_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _session_user(token: Optional[str]):
    if not token:
        return None
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?", (token, now)).fetchone()
    return row


async def require_session(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get(SESSION_COOKIE)
    user = _session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def _active_subscription(user_id: int):
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT s.*,p.name,p.price,p.requests FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.user_id=? AND s.status='active' AND s.expires>? ORDER BY s.expires DESC LIMIT 1", (user_id, now)).fetchone()
    return row


@app.post("/auth/register")
async def register(payload: Dict[str, Any]):
    email=str(payload.get("email","")).strip().lower(); password=str(payload.get("password","")); name=str(payload.get("name","")).strip()[:80]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email): raise HTTPException(400,"Valid email required")
    if len(password)<6: raise HTTPException(400,"Password must be at least 6 characters")
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cur=conn.execute("INSERT INTO users(email,name,password_hash,created_time) VALUES(?,?,?,?)",(email,name,_hash_password(password),time.time())); uid=cur.lastrowid
            free=conn.execute("SELECT id FROM plans WHERE name='Free'").fetchone()[0]
            conn.execute("INSERT INTO subscriptions(user_id,plan_id,started,expires,requests_used,status) VALUES(?,?,?,?,0,'active')",(uid,free,time.time(),time.time()+365*86400))
            conn.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,"An account with this email already exists")
    token=secrets.token_urlsafe(40)
    with sqlite3.connect(DB_FILE) as conn: conn.execute("INSERT INTO sessions VALUES(?,?,?)",(token,uid,time.time()+30*86400)); conn.commit()
    return {"success":True,"token":token}


@app.post("/auth/login")
async def login(payload: Dict[str, Any]):
    email=str(payload.get("email","")).strip().lower(); password=str(payload.get("password",""))
    with sqlite3.connect(DB_FILE) as conn: row=conn.execute("SELECT id,password_hash FROM users WHERE email=?",(email,)).fetchone()
    if not row or not _check_password(password,row[1]): raise HTTPException(401,"Invalid email or password")
    token=secrets.token_urlsafe(40)
    with sqlite3.connect(DB_FILE) as conn: conn.execute("INSERT INTO sessions VALUES(?,?,?)",(token,row[0],time.time()+30*86400)); conn.commit()
    return {"success":True,"token":token}


@app.get("/plans")
async def plans():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; rows=conn.execute("SELECT id,name,price,requests FROM plans WHERE active=1 ORDER BY price").fetchall()
    return {"plans":[dict(r) for r in rows]}


@app.get("/me")
async def me(user=Depends(require_session)):
    uid=user[0]; sub=_active_subscription(uid)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; keys=conn.execute("SELECT name,key,created_time FROM user_keys WHERE user_id=? AND revoked=0 ORDER BY id DESC",(uid,)).fetchall()
        used=conn.execute("SELECT COALESCE(SUM(CASE WHEN type='api' THEN amount ELSE 0 END),0) FROM wallet_transactions WHERE user_id=?",(uid,)).fetchone()[0]
    return {"email":user[1],"name":user[2],"wallet":user[4],"plan":sub[7] if sub else "Free","requests_used":sub[5] if sub else 0,"request_limit":sub[9] if sub else 100,"keys":[dict(k) for k in keys]}


@app.post("/keys")
async def create_key(payload: Dict[str, Any], user=Depends(require_session)):
    sub=_active_subscription(user[0])
    if not sub: raise HTTPException(402,"An active subscription is required")
    key="vba_"+secrets.token_urlsafe(30)
    name=str(payload.get("name","API Key")).strip()[:80] or "API Key"
    with sqlite3.connect(DB_FILE) as conn: conn.execute("INSERT INTO user_keys(user_id,name,key,created_time) VALUES(?,?,?,?)",(user[0],name,key,time.time())); conn.commit()
    return {"success":True,"name":name,"key":key}


@app.get("/usage")
async def usage(user=Depends(require_session)):
    sub=_active_subscription(user[0])
    return {"plan":sub[7] if sub else "Free","requests_used":sub[5] if sub else 0,"request_limit":sub[9] if sub else 100}


@app.post("/wallet/deposit")
async def wallet_deposit(payload: Dict[str, Any], user=Depends(require_session)):
    amount=float(payload.get("amount",0)); ref=str(payload.get("reference","")).strip()[:120]
    if amount<1 or amount>100000: raise HTTPException(400,"Amount must be between ₹1 and ₹100,000")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO wallet_transactions(user_id,amount,type,status,reference,created_time) VALUES(?,?, 'deposit','pending',?,?)",(user[0],amount,ref,time.time())); conn.commit()
    return {"success":True,"status":"pending","message":"Payment request created. Wallet is credited only after a verified payment webhook."}


@app.post("/payments/webhook")
async def payment_webhook(request: Request):
    raw=await request.body(); signature=request.headers.get("X-Payment-Signature","")
    if PAYMENT_WEBHOOK_SECRET:
        expected=hmac.new(PAYMENT_WEBHOOK_SECRET.encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature): raise HTTPException(401,"Invalid payment signature")
    try: data=json.loads(raw.decode())
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower(); amount=float(data.get("amount",0)); ref=str(data.get("reference",data.get("payment_id",""))).strip()
    status=str(data.get("status","")).lower()
    if status not in {"success","paid","captured"} or amount<=0 or not email or not ref: raise HTTPException(400,"Webhook requires successful status, email, amount and reference")
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; u=conn.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()
        if not u: raise HTTPException(404,"User not found")
        exists=conn.execute("SELECT id FROM wallet_transactions WHERE reference=?",(ref,)).fetchone()
        if exists: return {"success":True,"duplicate":True}
        conn.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(amount,u[0])); conn.execute("INSERT INTO wallet_transactions(user_id,amount,type,status,reference,created_time) VALUES(?,?, 'deposit','success',?,?)",(u[0],amount,ref,time.time())); conn.commit()
    return {"success":True,"credited":amount}


@app.post("/wallet/purchase")
async def wallet_purchase(payload: Dict[str, Any], user=Depends(require_session)):
    plan_id=int(payload.get("plan_id",0)); now=time.time()
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; p=conn.execute("SELECT * FROM plans WHERE id=? AND active=1",(plan_id,)).fetchone()
        if not p: raise HTTPException(404,"Plan not found")
        u=conn.execute("SELECT wallet FROM users WHERE id=?",(user[0],)).fetchone()
        if u[0] < p[2]: raise HTTPException(402,"Insufficient wallet balance")
        conn.execute("UPDATE users SET wallet=wallet-? WHERE id=?",(p[2],user[0])); conn.execute("UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active'",(user[0],)); conn.execute("INSERT INTO subscriptions(user_id,plan_id,started,expires,requests_used,status) VALUES(?,?,?,?,0,'active')",(user[0],p[0],now,now+30*86400)); conn.execute("INSERT INTO wallet_transactions(user_id,amount,type,status,reference,created_time) VALUES(?,?, 'subscription','success',?,?)",(user[0],-p[2],f"PLAN-{p[0]}-{int(now)}",now)); conn.commit()
    return {"success":True,"message":f"{p[1]} subscription activated for 30 days"}


@app.get("/wallet/transactions")
async def wallet_transactions(user=Depends(require_session)):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; rows=conn.execute("SELECT amount,type,status,reference,created_time FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT 100",(user[0],)).fetchall()
    return {"transactions":[dict(r) for r in rows]}


# =========================================================
# ADMIN
# =========================================================

async def require_admin(request: Request):
    user=await require_session(request)
    if not ADMIN_EMAIL or str(user[1]).lower()!=ADMIN_EMAIL:
        raise HTTPException(403,"Admin access required")
    return user


@app.get("/admin/overview")
async def admin_overview(user=Depends(require_admin)):
    with sqlite3.connect(DB_FILE) as conn:
        users=conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        payments=conn.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_transactions WHERE type='deposit' AND status='success'").fetchone()[0]
        subs=conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires>?",(time.time(),)).fetchone()[0]
        pending=conn.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_transactions WHERE type='deposit' AND status='pending'").fetchone()[0]
    return {"users":users,"payments":payments,"active_subscriptions":subs,"pending_deposits":pending}


@app.get("/admin/deposits")
async def admin_deposits(user=Depends(require_admin)):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory=sqlite3.Row; rows=conn.execute("SELECT w.*,u.email FROM wallet_transactions w JOIN users u ON u.id=w.user_id WHERE w.type='deposit' ORDER BY w.id DESC LIMIT 200").fetchall()
    return {"deposits":[dict(r) for r in rows]}


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
async def health_check():

    return {

        "status":
            "healthy",

        "version":
            "2.3.0",

        "yt_dlp_version":
            yt_dlp.version.__version__,

        "cache_expiry_hours":
            CACHE_EXPIRE_HOURS
    }


# =========================================================
# SEARCH
# =========================================================

@app.get("/search")
async def search_youtube_music(

    _: bool = Depends(require_api_key),

    q: str = Query(
        ...,
        description="Search query"
    ),

    limit: int = Query(
        1,
        description=
            "Number of results to return (max 20)"
    )
):

    try:

        logger.info(
            f"Received search request "
            f"for query '{q}' "
            f"with limit {limit}"
        )

        actual_limit = min(
            max(
                1,
                limit
            ),
            20
        )

        def perform_search():

            return ytmusic.search(
                q,
                filter="songs",
                limit=actual_limit
            )

        results = await asyncio.to_thread(
            perform_search
        )

        formatted_results = []

        for r in results:

            artists = ", ".join(
                [
                    a.get(
                        "name",
                        ""
                    )
                    for a in r.get(
                        "artists",
                        []
                    )
                ]
            )

            thumbnails = r.get(
                "thumbnails",
                []
            )

            thumbnail_url = (
                thumbnails[-1].get(
                    "url"
                )
                if thumbnails
                else None
            )

            formatted_results.append({

                "title":
                    r.get("title"),

                "artist":
                    artists,

                "videoId":
                    r.get("videoId"),

                "duration":
                    r.get("duration"),

                "thumbnail":
                    thumbnail_url
            })

        logger.info(
            f"Successfully completed search "
            f"for query '{q}', "
            f"returned "
            f"{len(formatted_results)} "
            f"result(s)"
        )

        if actual_limit == 1:

            return (
                formatted_results[0]
                if formatted_results
                else {}
            )

        return formatted_results

    except Exception as e:

        logger.error(
            f"Search error for query '{q}': {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Search failed",

                "message":
                    str(e)
            }
        )


# =========================================================
# THUMBNAIL API
# =========================================================

@app.get("/thumbnail")
async def get_thumbnail(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL"
    )
):

    try:

        result = await asyncio.to_thread(
            fetch_thumbnail_sync,
            url
        )

        return result

    except Exception as e:

        logger.error(
            f"Thumbnail API error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Failed to fetch thumbnail",

                "message":
                    str(e)
            }
        )


# =========================================================
# AUDIO / VIDEO DOWNLOAD API
# =========================================================

@app.get("/download")
async def download_media(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL or video ID"
    ),

    type: str = Query(
        "audio",
        description="Media type: audio or video"
    ),

    response: str = Query(
        "file",
        description="Response mode: file or json"
    )
):
    """Download audio or video and return the actual media by default.

    /download?url=VIDEO_ID&type=audio -> MP3
    /download?url=VIDEO_ID&type=video -> MP4
    Add response=json when metadata JSON is required.
    """

    media_type = type.strip().lower()
    response_mode = response.strip().lower()

    if media_type not in {"audio", "video"}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid media type",
                "message": "type must be either 'audio' or 'video'"
            }
        )

    if response_mode not in {"file", "json"}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid response mode",
                "message": "response must be either 'file' or 'json'"
            }
        )

    try:
        downloader = (
            download_audio_sync
            if media_type == "audio"
            else download_video_sync
        )

        result = await asyncio.to_thread(
            downloader,
            url
        )

        if response_mode == "json":
            return JSONResponse(content=result)

        file_path = result.get("path")
        filename = result.get("filename") or os.path.basename(file_path or "")

        if not file_path or not os.path.isfile(file_path):
            logger.error(
                f"{media_type.title()} result points to missing file: {file_path}"
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"{media_type.title()} file not found",
                    "message": (
                        f"The cached/downloaded {media_type} file is no longer available. "
                        "A new download may be required."
                    )
                }
            )

        size = os.path.getsize(file_path)
        if size <= 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": f"Invalid {media_type} file",
                    "message": "The downloaded file is empty."
                }
            )

        if media_type == "audio":
            content_type = "audio/mpeg"
            title_header = "X-Audio-Title"
        else:
            ext = os.path.splitext(filename)[1].lower()
            content_type = {
                ".mp4": "video/mp4",
                ".webm": "video/webm",
                ".mkv": "video/x-matroska"
            }.get(ext, "video/mp4")
            title_header = "X-Video-Title"

        logger.info(
            f"Serving {media_type} file: {filename} ({size} bytes)"
        )

        return FileResponse(
            path=file_path,
            filename=filename,
            media_type=content_type,
            headers={
                "X-Video-ID": str(result.get("videoId") or ""),
                title_header: str(result.get("title") or "")[:500],
                "X-Media-Type": media_type,
                "X-API-Response": "file"
            }
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(
            f"{media_type.title()} download API error: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"{media_type.title()} download failed",
                "message": str(e)
            }
        )


# =========================================================

# VIDEO DOWNLOAD API
# =========================================================

@app.get("/video")
async def download_video(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL"
    )
):

    try:

        result = await asyncio.to_thread(
            download_video_sync,
            url
        )

        return JSONResponse(
            content=result
        )

    except Exception as e:

        logger.error(
            f"Video download API error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Video download failed",

                "message":
                    str(e)
            }
        )


# =========================================================
# FILE SERVING
# =========================================================

@app.get("/files/{filename}")
async def get_file(
    filename: str,
    _: bool = Depends(require_api_key)
):

    filename = os.path.basename(
        filename
    )

    file_path = os.path.join(
        DOWNLOAD_DIR,
        filename
    )

    if not os.path.isfile(
        file_path
    ):

        logger.warning(
            f"Requested file not found: "
            f"{filename}"
        )

        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    return FileResponse(
        path=file_path,
        filename=filename
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False
    )