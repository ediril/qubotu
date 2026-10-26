import os
import secrets
from pathlib import Path

HOME = Path(os.environ.get("QUBOTU_HOME", Path.home() / ".qubotu"))
DB_PATH = HOME / "qubotu.db"
AGENTS_DIR = HOME / "agents"
MODELS_DIR = HOME / "models"
BROWSER_DIR = HOME / "browser"
LOG_PATH = HOME / "daemon.log"
UI_KEY_PATH = HOME / "ui.key"

HOST = "127.0.0.1"
PORT = int(os.environ.get("QUBOTU_PORT", "8765"))

# How often the scheduler checks for due runs, in seconds.
SCHEDULER_TICK = 20
# Maximum number of agent turns running at the same time.
MAX_CONCURRENT_TURNS = 3
# Requests between agents can chain; this caps how deep.
MAX_CHAIN_DEPTH = 2
# A single turn is abandoned after this many seconds.
TURN_TIMEOUT = 30 * 60
# After interrupting a timed-out turn, how long to wait for Codex to confirm it stopped.
INTERRUPT_GRACE = 15

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"


def ui_key() -> str:
    """The secret the user's browser holds to use Qubotu's UI. It lives in the data folder, which
    agents can't read, so an agent on this machine can't call the UI's controls."""
    try:
        return UI_KEY_PATH.read_text().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        fd = os.open(UI_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        return key


def ui_url(port: int = PORT) -> str:
    """The address that lets a browser in: it carries the key once, and Qubotu keeps it in a cookie."""
    return f"http://{HOST}:{port}/?key={ui_key()}"


def ensure_dirs() -> None:
    for d in (HOME, AGENTS_DIR, MODELS_DIR, BROWSER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Conversations, memory, and an API key live here: the owner only.
    HOME.chmod(0o700)
