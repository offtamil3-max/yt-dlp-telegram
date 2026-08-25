import base64
import datetime
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from queue import Queue
from typing import Any, Callable
from urllib.parse import urlparse

import telebot
import yt_dlp
from cryptography.fernet import Fernet
from telebot import apihelper, types
from telebot.util import quick_markup
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

import config

whitelist = getattr(config, "whitelist", None)
blacklist = getattr(config, "blacklist", None)
logs = getattr(config, "logs", None)
js_runtime = getattr(config, "js_runtime", None)
max_filesize = getattr(config, "max_filesize", 50000000)
max_user_concurrent_downloads = getattr(config, "max_user_concurrent_downloads", 1)
max_global_concurrent_downloads = getattr(config, "max_global_concurrent_downloads", 2)
max_retries = getattr(config, "max_retries", 3)
retry_delay = getattr(config, "retry_delay", 5)
allowed_domains = getattr(config, "allowed_domains", [])
forward_to: int | None = getattr(config, "forward_to", None)
forward_permissions: list[int] = getattr(config, "forward_permissions", [])

if max_user_concurrent_downloads < 1:
    max_user_concurrent_downloads = 1
if max_global_concurrent_downloads < 1:
    max_global_concurrent_downloads = 1

os.makedirs(config.output_folder, exist_ok=True)

key = hashlib.sha256(config.secret_key.encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(script_dir, "db.db")


def init_db() -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_cookies (
                user_id INTEGER PRIMARY KEY,
                cookie_data TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


init_db()


def db_query(query: str, params: tuple = (), fetchone: bool = False, commit: bool = False) -> Any:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        if commit:
            conn.commit()
        return cursor.fetchone() if fetchone else cursor.fetchall()
    finally:
        conn.close()


apihelper.CONNECT_TIMEOUT = 30
apihelper.READ_TIMEOUT = 30
bot = telebot.TeleBot(config.token)

last_edited = {}
download_queue: Queue[dict] = Queue()
queue_lock = threading.Lock()
active_global_downloads = 0
active_user_downloads: dict[int, int] = {}
queued_user_downloads: dict[int, int] = {}
_format_registry: dict[str, str] = {}
_format_counter = 0
format_lock = threading.Lock()

# Temporary files used by the new "Upload to Channel" button.
pending_channel_uploads: dict[str, dict[str, Any]] = {}
pending_lock = threading.Lock()
PENDING_FILE_TTL = 30 * 60


def _schedule_pending_cleanup(upload_id: str) -> None:
    def cleanup():
        time.sleep(PENDING_FILE_TTL)
        with pending_lock:
            item = pending_channel_uploads.pop(upload_id, None)
        if item:
            path = item.get("path")
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    threading.Thread(target=cleanup, daemon=True).start()


def encrypt_cookie(cookie_data: str) -> str:
    return cipher.encrypt(cookie_data.encode()).decode()


def decrypt_cookie(encrypted_data: str) -> str:
    return cipher.decrypt(encrypted_data.encode()).decode()


def youtube_url_validation(url):
    youtube_regex = (
        r"(https?://)?(www\.|m\.)?"
        r"(youtube|youtu|youtube-nocookie)\.(com|be)/"
        r"(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})"
    )
    return re.match(youtube_regex, url)


def is_allowed_domain(url):
    try:
        domain = urlparse(url).netloc.lower().split(":")[0]
        return domain in allowed_domains
    except (ValueError, AttributeError):
        return False


@bot.message_handler(commands=["start", "help"])
def start_command(message):
    bot.reply_to(
        message,
        "*Send me a video link* and I'll download it for you.\n\n"
        "Supported: *YouTube, TikTok, Instagram, Twitter/X and Bluesky*.\n\n"
        "Use `/image <url>` for image/gallery downloads.",
        parse_mode="MARKDOWN",
        disable_web_page_preview=True,
    )


def _validate_url(message, url: str, image: bool = False) -> bool:
    if image:
        allowed_img = getattr(config, "allowed_image_domains", None)
        if allowed_img is not None:
            try:
                domain = urlparse(url).netloc.lower().split(":")[0]
                if not any(domain == d or domain.endswith("." + d) for d in allowed_img):
                    bot.reply_to(message, "Invalid URL. This domain is not allowed for image downloads.")
                    return False
            except (ValueError, AttributeError):
                bot.reply_to(message, "Invalid URL.")
                return False
        return True

    if not is_allowed_domain(url):
        bot.reply_to(
            message,
            "Invalid URL. Only YouTube, TikTok, Instagram, Twitter and Bluesky links are supported.",
        )
        return False

    domain = urlparse(url).netloc.lower().split(":")[0]
    if domain in {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com", "youtube-nocookie.com"}:
        if not youtube_url_validation(url):
            bot.reply_to(message, "Invalid URL")
            return False

    return True


def _make_progress_hook(message, msg) -> Callable:
    def progress(d):
        if d["status"] != "downloading":
            return
        try:
            last = last_edited.get(f"{message.chat.id}-{msg.message_id}")
            if last and (datetime.datetime.now() - last).total_seconds() < 5:
                return

            downloaded_bytes = d.get("downloaded_bytes", 0)
            if downloaded_bytes > max_filesize:
                raise DownloadCancelled("File too large")

            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            perc = round(downloaded_bytes * 100 / total) if total else 0

            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=f"Downloading {d.get('info_dict', {}).get('title', 'media')}\n\n{perc}%",
            )
            last_edited[f"{message.chat.id}-{msg.message_id}"] = datetime.datetime.now()
        except DownloadCancelled:
            raise
        except Exception as e:
            print(e)

    return progress


class MissingInfoError(Exception):
    pass


def _channel_markup(upload_id: str):
    if forward_to is None or not forward_permissions:
        return None

    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    "📢 Upload to Channel",
                    callback_data=f"channel:{upload_id}",
                )
            ]
        ]
    )


def _send_media(message, info: Any, audio: bool, forward: bool = False) -> str:
    downloads = info.get("requested_downloads") or None

    if not downloads and info.get("entries"):
        downloads = info["entries"][0].get("requested_downloads") or None

    if not downloads:
        raise MissingInfoError("No requested downloads found")

    filepath = downloads[0]["filepath"]

    if not os.path.exists(filepath):
        raise MissingInfoError("Downloaded file does not exist")

    # /forward command or authorized automatic forwarding.
    if forward:
        if forward_to is None:
            raise RuntimeError("forward_to is not configured")

        with open(filepath, "rb") as f:
            if audio:
                bot.send_audio(forward_to, f)
            else:
                bot.send_video(
                    forward_to,
                    f,
                    width=downloads[0].get("width"),
                    height=downloads[0].get("height"),
                    caption=info.get("title", "Video"),
                )
        return filepath

    # Normal download: send to user and attach ONE Channel button.
    upload_id = uuid.uuid4().hex

    with pending_lock:
        pending_channel_uploads[upload_id] = {
            "path": filepath,
            "user_id": message.from_user.id,
            "title": info.get("title", "Video"),
            "audio": audio,
        }

    markup = _channel_markup(upload_id)

    with open(filepath, "rb") as f:
        if audio:
            bot.send_audio(
                message.chat.id,
                f,
                caption=info.get("title", "Audio"),
                reply_to_message_id=message.message_id,
                reply_markup=markup,
            )
        else:
            bot.send_video(
                message.chat.id,
                f,
                width=downloads[0].get("width"),
                height=downloads[0].get("height"),
                caption=info.get("title", "Video"),
                reply_to_message_id=message.message_id,
                reply_markup=markup,
            )

    _schedule_pending_cleanup(upload_id)
    return filepath


def send_image_group(chat_id: int, filepaths: list[str], reply_to_message_id: int | None = None) -> None:
    if not filepaths:
        return

    if len(filepaths) == 1:
        path = filepaths[0]
        ext = os.path.splitext(path)[1].lower()
        is_video = ext in [".mp4", ".webm", ".gif", ".mov"]

        with open(path, "rb") as f:
            if is_video:
                bot.send_video(chat_id, f, reply_to_message_id=reply_to_message_id)
            else:
                bot.send_photo(chat_id, f, reply_to_message_id=reply_to_message_id)
        return

    for i in range(0, len(filepaths), 10):
        chunk = filepaths[i:i + 10]
        opened_files = []
        media = []

        try:
            for path in chunk:
                ext = os.path.splitext(path)[1].lower()
                is_video = ext in [".mp4", ".webm", ".gif", ".mov"]
                f = open(path, "rb")
                opened_files.append(f)

                if is_video:
                    media.append(types.InputMediaVideo(f))
                else:
                    media.append(types.InputMediaPhoto(f))

            bot.send_media_group(
                chat_id,
                media,
                reply_to_message_id=reply_to_message_id,
            )
        finally:
            for f in opened_files:
                f.close()


def _cleanup(video_title: int) -> None:
    for file in os.listdir(config.output_folder):
        if file.startswith(str(video_title)):
            try:
                os.remove(os.path.join(config.output_folder, file))
            except OSError:
                pass


def _is_transient_error(e: Exception) -> bool:
    if isinstance(e, DownloadCancelled):
        return False

    err = str(e).lower()

    if any(p in err for p in ["rate-limit", "rate limit", "too many requests", "429"]):
        return True

    if "[youtube]" in err and "sign in" in err:
        return True

    if "login required" in err:
        return True

    if "http error 5" in err:
        return True

    return any(
        p in err
        for p in [
            "timeout",
            "connection reset",
            "connection refused",
            "connection closed",
            "eof",
            "name resolution",
        ]
    )


def check_url(content: str, message, image: bool = False) -> dict:
    match = re.search(r"https?://\S+", content or "")
    url = match.group(0) if match else content

    if not url or not urlparse(url).scheme:
        bot.reply_to(message, "Invalid URL")
        return {"success": False}

    if not _validate_url(message, url, image):
        return {"success": False}

    return {"success": True, "url": url}


def enqueue_download(
    message,
    content,
    audio: bool = False,
    format_id: str = "mp4",
    forward: bool = False,
    image: bool = False,
) -> None:
    forbidden = False

    if whitelist is not None and message.from_user.id not in whitelist:
        forbidden = True

    if blacklist is not None and message.from_user.id in blacklist:
        forbidden = True

    if forbidden:
        bot.reply_to(message, "You are not allowed to use this bot")
        return

    if not content:
        bot.reply_to(message, "Invalid URL")
        return

    check = check_url(content, message, image)

    if not check["success"]:
        return

    url = check["url"]
    user_id = message.from_user.id

    with queue_lock:
        pending = (
            active_user_downloads.get(user_id, 0)
            + queued_user_downloads.get(user_id, 0)
        )

        if pending >= max_user_concurrent_downloads:
            bot.reply_to(
                message,
                f"Too many concurrent downloads. Limit is {max_user_concurrent_downloads}.",
            )
            return

        queued_user_downloads[user_id] = (
            queued_user_downloads.get(user_id, 0) + 1
        )

        should_notify_queue = (
            active_global_downloads >= max_global_concurrent_downloads
            or download_queue.qsize() > 0
        )

        position = download_queue.qsize() + 1

        download_queue.put(
            {
                "message": message,
                "url": url,
                "audio": audio,
                "format_id": format_id,
                "forward": forward,
                "user_id": user_id,
                "image": image,
            }
        )

    if should_notify_queue:
        bot.reply_to(
            message,
            f"All download slots are busy. Your request has been queued (position {position}).",
        )


def _perform_download(
    message,
    url: str,
    audio: bool = False,
    format_id: str = "mp4",
    forward: bool = False,
    image: bool = False,
) -> None:
    msg = bot.reply_to(
        message,
        "Downloading image(s)..." if image else "Downloading...",
    )

    video_title = round(time.time() * 1000)

    ydl_opts: yt_dlp._Params = {
        "format": format_id,
        "outtmpl": f"{config.output_folder}/{video_title}.%(ext)s",
        "progress_hooks": [_make_progress_hook(message, msg)],
        "max_filesize": max_filesize,
        "socket_timeout": 30,
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
            if audio
            else []
        ),
    }

    if js_runtime is not None:
        ydl_opts["js_runtimes"] = js_runtime
        ydl_opts["remote_components"] = {"ejs:github"}

    cookie_file = None
    keep_file_for_button = False

    try:
        user_id = message.from_user.id

        result = db_query(
            "SELECT cookie_data FROM user_cookies WHERE user_id = ?",
            (user_id,),
            fetchone=True,
        )

        if result:
            decrypted_data = decrypt_cookie(result[0])
            cookie_file = f"{config.output_folder}/cookies_{user_id}.txt"

            with open(cookie_file, "w") as f:
                f.write(decrypted_data)

            ydl_opts["cookiefile"] = cookie_file

        if image:
            cmd = [
                sys.executable,
                "-m",
                "gallery_dl",
                "-D",
                config.output_folder,
                "-f",
                f"{video_title}_{{num|id}}.{{extension}}",
                "--Print",
                "{filepath}",
                url,
            ]

            if cookie_file:
                cmd.extend(["--cookies", cookie_file])

            result_proc = None

            for attempt in range(max_retries + 1):
                try:
                    result_proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    break
                except subprocess.CalledProcessError as e:
                    has_files = any(
                        f.startswith(str(video_title))
                        for f in os.listdir(config.output_folder)
                    )

                    if has_files:
                        result_proc = e
                        break

                    if attempt < max_retries:
                        _cleanup(video_title)
                        time.sleep(retry_delay)
                    else:
                        raise

            downloaded_files = []

            if result_proc and result_proc.stdout:
                for line in result_proc.stdout.splitlines():
                    line = line.strip()

                    if (
                        line
                        and os.path.exists(line)
                        and os.path.basename(line).startswith(str(video_title))
                    ):
                        downloaded_files.append(line)

            if not downloaded_files:
                downloaded_files = [
                    os.path.join(config.output_folder, f)
                    for f in os.listdir(config.output_folder)
                    if f.startswith(str(video_title))
                ]

            if not downloaded_files:
                raise MissingInfoError("No downloaded images found")

            bot.edit_message_text(
                "Sending file(s) to Telegram...",
                message.chat.id,
                msg.message_id,
            )

            send_image_group(
                message.chat.id,
                downloaded_files,
                reply_to_message_id=message.message_id,
            )

            bot.delete_message(message.chat.id, msg.message_id)

        else:
            info = None

            for attempt in range(max_retries + 1):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    break

                except (DownloadError, ExtractorError, DownloadCancelled) as e:
                    if _is_transient_error(e) and attempt < max_retries:
                        _cleanup(video_title)
                        print(
                            f"Retry {attempt + 1}/{max_retries} for {url}: {e}"
                        )
                        time.sleep(retry_delay)
                        continue

                    raise

            bot.delete_message(message.chat.id, msg.message_id)

            _send_media(message, info, audio, forward)

            if (
                not forward
                and forward_to is not None
                and forward_permissions
            ):
                keep_file_for_button = True

    except MissingInfoError as e:
        try:
            bot.edit_message_text(
                f"Couldn't find media files: {e}",
                message.chat.id,
                msg.message_id,
            )
        except Exception:
            pass

    except (
        DownloadError,
        ExtractorError,
        subprocess.CalledProcessError,
    ) as e:
        err = str(e).lower()

        if "[youtube]" in err and "sign in" in err:
            text = (
                "YouTube is rate limiting third-party downloaders right now. "
                "Try again later."
            )
        elif "login required" in err or "rate-limit reached" in err:
            text = "Content not available (rate limit or login required)."
        else:
            text = (
                "There was an error downloading the media. "
                "Please try again later."
            )

        try:
            bot.edit_message_text(
                text,
                message.chat.id,
                msg.message_id,
            )
        except Exception:
            pass

    except DownloadCancelled:
        try:
            bot.edit_message_text(
                f"Download cancelled — file exceeds "
                f"{round(max_filesize / 1_000_000)}MB limit.",
                message.chat.id,
                msg.message_id,
            )
        except Exception:
            pass

    except Exception as e:
        print(f"Unexpected error for {url}: {e}")

        try:
            bot.edit_message_text(
                "An unexpected error occurred. Please try again later.",
                message.chat.id,
                msg.message_id,
            )
        except Exception:
            pass

    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass

        if not keep_file_for_button:
            _cleanup(video_title)

        last_edited.pop(
            f"{message.chat.id}-{msg.message_id}",
            None,
        )


def _download_worker() -> None:
    global active_global_downloads

    while True:
        task = download_queue.get()
        user_id = task.get("user_id")

        if not user_id:
            download_queue.task_done()
            continue

        incremented = False

        try:
            with queue_lock:
                active_global_downloads += 1

                active_user_downloads[user_id] = (
                    active_user_downloads.get(user_id, 0) + 1
                )

                queued_user_downloads[user_id] = max(
                    0,
                    queued_user_downloads.get(user_id, 0) - 1,
                )

                if queued_user_downloads[user_id] == 0:
                    del queued_user_downloads[user_id]

                incremented = True

            _perform_download(
                task["message"],
                task["url"],
                task["audio"],
                task["format_id"],
                task["forward"],
                task.get("image", False),
            )

        except Exception as e:
            print(
                f"Error in download worker for task from user "
                f"{user_id}: {e}"
            )

        finally:
            if incremented:
                with queue_lock:
                    active_global_downloads -= 1

                    active_user_downloads[user_id] = max(
                        0,
                        active_user_downloads.get(user_id, 0) - 1,
                    )

                    if active_user_downloads[user_id] == 0:
                        del active_user_downloads[user_id]

            download_queue.task_done()


def log(message, text: str, media: str):
    if logs:
        if message.chat.type == "private":
            chat_info = "Private chat"
        else:
            chat_info = (
                f"Group: *{message.chat.title}* "
                f"(`{message.chat.id}`)"
            )

        bot.send_message(
            logs,
            f"Download request ({media}) from "
            f"@{message.from_user.username} "
            f"({message.from_user.id})\n\n"
            f"{chat_info}\n\n{text}",
        )


def get_text(message):
    if not message.text:
        if message.reply_to_message and message.reply_to_message.text:
            return message.reply_to_message.text
        return None

    parts = message.text.split(" ", 1)

    if len(parts) < 2:
        if message.reply_to_message and message.reply_to_message.text:
            return message.reply_to_message.text
        return None

    return parts[1].strip()


@bot.message_handler(commands=["download"])
def download_command(message):
    text = get_text(message)

    if not text:
        bot.reply_to(
            message,
            "Invalid usage, use `/download url`",
            parse_mode="MARKDOWN",
        )
        return

    log(message, text, "video")
    enqueue_download(message, text)


@bot.message_handler(commands=["audio"])
def download_audio_command(message):
    text = get_text(message)

    if not text:
        bot.reply_to(
            message,
            "Invalid usage, use `/audio url`",
            parse_mode="MARKDOWN",
        )
        return

    log(message, text, "audio")
    enqueue_download(message, text, True)


@bot.message_handler(commands=["image"])
def download_image_command(message):
    text = get_text(message)

    if not text:
        bot.reply_to(
            message,
            "Invalid usage, use `/image url`",
            parse_mode="MARKDOWN",
        )
        return

    log(message, text, "image")
    enqueue_download(message, text, image=True)


@bot.message_handler(commands=["forward"])
def forward_command(message):
    if message.from_user.id not in forward_permissions:
        bot.reply_to(
            message,
            "You are not allowed to forward videos",
        )
        return

    if not forward_to:
        bot.reply_to(
            message,
            "forward_to is not set",
        )
        return

    text = get_text(message)

    if not text:
        bot.reply_to(
            message,
            "Invalid usage, use `/forward url`",
            parse_mode="MARKDOWN",
        )
        return

    log(message, text, "video")
    enqueue_download(message, text, forward=True)


@bot.message_handler(commands=["custom"])
def custom(message):
    forbidden = False

    if whitelist is not None and message.from_user.id not in whitelist:
        forbidden = True

    if blacklist is not None and message.from_user.id in blacklist:
        forbidden = True

    if forbidden:
        bot.reply_to(
            message,
            "You are not allowed to use this bot",
        )
        return

    text = message.text if message.text else message.caption

    check = check_url(text, message)

    if not check["success"]:
        return

    url = check["url"]

    msg = bot.reply_to(
        message,
        "Getting formats...",
    )

    try:
        with yt_dlp.YoutubeDL(
            {"socket_timeout": 30}
        ) as ydl:
            info = ydl.extract_info(
                url,
                download=False,
            )

    except Exception:
        bot.edit_message_text(
            "Failed to fetch formats. Please try again later.",
            message.chat.id,
            msg.message_id,
        )
        return

    formats = info.get("formats") or []

    global _format_registry, _format_counter

    data = {}

    with format_lock:
        while len(_format_registry) > 2000:
            first_key = next(iter(_format_registry))
            _format_registry.pop(first_key, None)

        for x in formats:
            if x.get("video_ext") == "none":
                continue

            resolution = x.get("resolution") or "unknown"
            ext = x.get("ext") or "unknown"
            label = f"{resolution}.{ext}"

            if label in data:
                label = (
                    f"{resolution}.{ext} "
                    f"({x.get('format_id')})"
                )

            fid = str(_format_counter)
            _format_counter += 1

            _format_registry[fid] = x["format_id"]
            data[label] = {
                "callback_data": fid
            }

    markup = quick_markup(
        data,
        row_width=2,
    )

    bot.delete_message(
        msg.chat.id,
        msg.message_id,
    )

    bot.reply_to(
        message,
        "Choose a format",
        reply_markup=markup,
    )


def filter_cookies_by_domain(cookie_data: str) -> str:
    lines = cookie_data.split("\n")
    filtered_lines = []

    all_allowed = set(allowed_domains)
    allowed_img = getattr(
        config,
        "allowed_image_domains",
        None,
    )

    if allowed_img:
        all_allowed.update(allowed_img)

    for line in lines:
        if line.startswith("#") or not line.strip():
            filtered_lines.append(line)
            continue

        parts = line.split("\t")

        if len(parts) < 7:
            continue

        domain = parts[0].lstrip(".")

        if any(
            domain == d or domain.endswith("." + d)
            for d in all_allowed
        ):
            filtered_lines.append(line)

    return "\n".join(filtered_lines)


@bot.message_handler(commands=["id"])
def get_chat_id(message):
    bot.reply_to(
        message,
        message.chat.id,
    )


@bot.message_handler(commands=["queue"])
def queue_command(message):
    bot.reply_to(
        message,
        f"Videos in queue: {download_queue.qsize()}",
    )


def is_cookie_command(message):
    text = message.text or message.caption or ""
    return text.startswith("/cookie") or text.startswith("/cookies")


@bot.message_handler(
    func=is_cookie_command,
    content_types=["document", "text"],
)
def handle_cookie(message):
    user_id = message.from_user.id

    if not message.document:
        result = db_query(
            "SELECT cookie_data "
            "FROM user_cookies "
            "WHERE user_id = ?",
            (user_id,),
            fetchone=True,
        )

        if result:
            cookie_file = (
                f"{config.output_folder}/"
                f"cookies_{user_id}_temp.txt"
            )

            try:
                decrypted_data = decrypt_cookie(
                    result[0]
                )

                with open(
                    cookie_file,
                    "w",
                ) as f:
                    f.write(decrypted_data)

                markup = types.InlineKeyboardMarkup()

                markup.add(
                    types.InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data="delete_cookies",
                    )
                )

                with open(
                    cookie_file,
                    "rb",
                ) as f:
                    bot.send_document(
                        message.chat.id,
                        f,
                        reply_to_message_id=message.message_id,
                        visible_file_name="cookies.txt",
                        reply_markup=markup,
                    )

            finally:
                if os.path.exists(cookie_file):
                    os.remove(cookie_file)

        else:
            bot.reply_to(
                message,
                "No cookies stored. Send a file with this command to store cookies.",
            )

        return

    file_info = bot.get_file(
        message.document.file_id
    )

    if not file_info.file_path:
        bot.reply_to(
            message,
            "Failed to get file information.",
        )
        return

    downloaded_file = bot.download_file(
        file_info.file_path
    )

    cookie_data = downloaded_file.decode(
        "utf-8"
    )

    filtered_cookie_data = filter_cookies_by_domain(
        cookie_data
    )

    encrypted_data = encrypt_cookie(
        filtered_cookie_data
    )

    db_query(
        "INSERT OR REPLACE INTO "
        "user_cookies (user_id, cookie_data) "
        "VALUES (?, ?)",
        (
            user_id,
            encrypted_data,
        ),
        commit=True,
    )

    bot.reply_to(
        message,
        "Cookies saved successfully!",
    )


@bot.message_handler(
    func=lambda m: True,
    content_types=[
        "text",
        "photo",
        "audio",
        "video",
        "document",
    ],
)
def handle_private_messages(message: types.Message):
    text = (
        message.text
        if message.text
        else message.caption
        if message.caption
        else None
    )

    if message.chat.type == "private":
        should_forward = (
            forward_to is not None
            and message.from_user.id in forward_permissions
        )

        log(
            message,
            text or "<no text>",
            "video",
        )

        enqueue_download(
            message,
            text,
            forward=should_forward,
        )


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    # ==========================================
    # NEW: ONE BUTTON -> TELEGRAM CHANNEL
    # ==========================================
    if call.data.startswith("channel:"):
        upload_id = call.data.split(
            ":",
            1,
        )[1]

        with pending_lock:
            item = pending_channel_uploads.get(
                upload_id
            )

        if not item:
            bot.answer_callback_query(
                call.id,
                "File expired. Download it again.",
                show_alert=True,
            )
            return

        if call.from_user.id != item["user_id"]:
            bot.answer_callback_query(
                call.id,
                "Only the user who downloaded this file can upload it.",
                show_alert=True,
            )
            return

        if forward_to is None:
            bot.answer_callback_query(
                call.id,
                "Channel is not configured.",
                show_alert=True,
            )
            return

        if (
            forward_permissions
            and call.from_user.id not in forward_permissions
        ):
            bot.answer_callback_query(
                call.id,
                "You are not allowed to upload to the channel.",
                show_alert=True,
            )
            return

        path = item["path"]

        if not os.path.exists(path):
            with pending_lock:
                pending_channel_uploads.pop(
                    upload_id,
                    None,
                )

            bot.answer_callback_query(
                call.id,
                "File expired. Download it again.",
                show_alert=True,
            )
            return

        try:
            bot.answer_callback_query(
                call.id,
                "Uploading to channel...",
            )

            with open(
                path,
                "rb",
            ) as f:
                if item.get("audio"):
                    bot.send_audio(
                        forward_to,
                        f,
                        caption=item.get(
                            "title",
                            "Audio",
                        ),
                    )
                else:
                    bot.send_video(
                        forward_to,
                        f,
                        caption=item.get(
                            "title",
                            "Video",
                        ),
                    )

            try:
                bot.edit_message_reply_markup(
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass

            bot.send_message(
                call.message.chat.id,
                "✅ Video uploaded to the channel.",
            )

            with pending_lock:
                pending_channel_uploads.pop(
                    upload_id,
                    None,
                )

            try:
                os.remove(path)
            except OSError:
                pass

        except Exception as e:
            print(
                "CHANNEL UPLOAD ERROR:",
                repr(e),
            )

            bot.answer_callback_query(
                call.id,
                "Channel upload failed.",
                show_alert=True,
            )

        return

    # ==========================================
    # DELETE COOKIES BUTTON
    # ==========================================
    if call.data == "delete_cookies":
        user_id = call.from_user.id

        db_query(
            "DELETE FROM user_cookies "
            "WHERE user_id = ?",
            (user_id,),
            commit=True,
        )

        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption="Cookies deleted successfully!",
                reply_markup=None,
            )
        except Exception:
            pass

        bot.answer_callback_query(
            call.id,
            "Cookies deleted!",
        )
        return

    # ==========================================
    # CUSTOM FORMAT BUTTONS
    # ==========================================
    if (
        call.message
        and call.message.reply_to_message
    ):
        if (
            call.from_user.id
            == call.message.reply_to_message.from_user.id
        ):
            url = get_text(
                call.message.reply_to_message
            )

            with format_lock:
                format_id = _format_registry.get(
                    call.data
                )

            if not format_id:
                bot.answer_callback_query(
                    call.id,
                    "Format no longer available",
                )
                return

            bot.delete_message(
                call.message.chat.id,
                call.message.message_id,
            )

            enqueue_download(
                call.message.reply_to_message,
                url,
                format_id=f"{format_id}+bestaudio",
            )
        else:
            bot.answer_callback_query(
                call.id,
                "You didn't send the request",
            )


def _start_download_workers() -> None:
    for i in range(
        max_global_concurrent_downloads
    ):
        worker = threading.Thread(
            target=_download_worker,
            name=f"download-worker-{i + 1}",
            daemon=True,
        )
        worker.start()


_start_download_workers()

print(
    f"ready as @{bot.user.username}"
)

bot.infinity_polling()
