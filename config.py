# Telegram bot token
token: str = "8825877138:AAEe81HJrQK0_GiaSOYn3UYxutXwQwqng4g"

# User access
whitelist: list[int] | None = None
blacklist: list[int] | None = None

# Logs
logs: int | None = None

# File size: 50 MB
max_filesize: int = 50000000

# Download limits
max_user_concurrent_downloads: int = 1
max_global_concurrent_downloads: int = 2

# Retry
max_retries: int = 3
retry_delay: int = 5

# Output
output_folder: str = "/tmp/satoru"

# Supported domains
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

# Gallery/image domains
allowed_image_domains: list[str] | None = None

# Cookie encryption key
secret_key: str = "change-this-to-a-random-secret-key"

# YouTube challenge runtime
js_runtime = None

# ==========================================
# TELEGRAM CHANNEL AUTO FORWARD
# ==========================================

forward_to: int | None = -1003605888839

forward_permissions: list[int] = [
    8523536642
]
