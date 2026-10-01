import os
import re
import time
import asyncio
import uuid
import sqlite3
import logging
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Header, Depends
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
    "2160"
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

# =========================================================
# VELOCITYBOTS PAID API AUTHENTICATION
# =========================================================
# Per-customer keys, subscriptions, wallet metering and manual billing live
# in billing.py. Existing endpoint paths and query parameters are preserved.
from billing import router as billing_router, require_api_key

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

            conn.commit()

        logger.info(
            "SQLite database initialized."
        )

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

app.include_router(billing_router)

# In-process job registry for downloads that may exceed Heroku's 30-second
# router timeout. Clients poll the status endpoint until the file is ready.
DOWNLOAD_JOBS: Dict[str, Dict[str, Any]] = {}
DOWNLOAD_JOBS_MAX = 500

async def _run_download_job(job_id: str, downloader, url: str, media_type: str) -> None:
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        return
    job["status"] = "processing"
    try:
        result = await asyncio.to_thread(downloader, url)
        job.update({
            "status": "completed",
            "result": result,
            "download_url": result.get("download_url"),
            "filename": result.get("filename"),
            "title": result.get("title"),
            "media_type": media_type,
            "completed_at": time.time(),
        })
    except Exception as exc:
        logger.exception("Background %s download job %s failed", media_type, job_id)
        job.update({"status": "failed", "error": str(exc), "completed_at": time.time()})


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

HTML_FILE = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "Magma.html"
)

try:

    with open(
        HTML_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        DEVELOPER_PORTAL_HTML = f.read()

    logger.info(
        "Magma.html loaded successfully."
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
            f"bestvideo[height<={MAX_VIDEO_QUALITY}]+bestaudio/"
            f"best[height<={MAX_VIDEO_QUALITY}]/best",

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

    if response_mode not in {"file", "json", "job"}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid response mode",
                "message": "response must be 'file', 'json', or 'job'"
            }
        )

    try:
        downloader = (
            download_audio_sync
            if media_type == "audio"
            else download_video_sync
        )

        if response_mode == "job":
            # Bound memory use; retain active jobs and recent completed jobs.
            if len(DOWNLOAD_JOBS) >= DOWNLOAD_JOBS_MAX:
                finished = [
                    key for key, value in DOWNLOAD_JOBS.items()
                    if value.get("status") in {"completed", "failed"}
                ]
                for key in finished[: max(1, len(DOWNLOAD_JOBS) - DOWNLOAD_JOBS_MAX + 1)]:
                    DOWNLOAD_JOBS.pop(key, None)
            job_id = uuid.uuid4().hex
            DOWNLOAD_JOBS[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "media_type": media_type,
                "created_at": time.time(),
            }
            asyncio.create_task(_run_download_job(job_id, downloader, url, media_type))
            return JSONResponse(
                status_code=202,
                content={
                    "status": "queued",
                    "job_id": job_id,
                    "status_url": f"/download/status/{job_id}",
                    "message": "Download started. Poll status_url until status is completed.",
                },
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
                # HTTP header values must not contain leading whitespace or control characters.
                title_header: " ".join(
                    "".join(
                        ch for ch in str(result.get("title") or "")
                        if 32 <= ord(ch) <= 126
                    ).split()
                )[:500] or "Audio",
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

# BACKGROUND DOWNLOAD JOB STATUS API
# =========================================================

@app.get("/download/status/{job_id}")
async def download_job_status(
    job_id: str,
    _: bool = Depends(require_api_key),
):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Download job not found",
                "message": "The job may have expired or this dyno may have restarted.",
            },
        )
    response_data = {key: value for key, value in job.items() if key != "result"}
    if job.get("status") == "completed":
        response_data["file_url"] = job.get("download_url")
    return JSONResponse(content=response_data)


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