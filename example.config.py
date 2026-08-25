# Telegram bot token
# IMPORTANT: use a NEW token if the old one was exposed publicly.
token: str = "8825877138:AAEe81HJrQK0_GiaSOYn3UYxutXwQwqng4g"

# Optional user access control
whitelist: list[int] | None = None
blacklist: list[int] | None = None

# Logs channel (optional)
logs: int | None = None

# Maximum file size in bytes
max_filesize: int = 50000000

# Maximum downloads per user
max_user_concurrent_downloads: int = 1

# Maximum downloads globally
max_global_concurrent_downloads: int = 2

# Retry settings
max_retries: int = 3
retry_delay: int = 5

# Download folder
output_folder: str = "/tmp/satoru"

# Supported video domains
allowed_domains: list[str] = [
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
    "youtube-nocookie.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "bsky.app",
    "www.bsky.app",
]

# Image/gallery domains
allowed_image_domains: list[str] | None = None

# Used to encrypt stored cookies
secret_key: str = "change-this-secret-key"

# YouTube challenge runtime
js_runtime: dict[str, dict[str, str] | None] | None = {
    "bun": {"path": "bun"}
}

# ==========================================
# YOUR TELEGRAM CHANNEL
# ==========================================
forward_to: int | None = -1003605888839

# Only these Telegram user IDs can use the
# "📢 Upload to Channel" button.
forward_permissions: list[int] = [8523536642]
