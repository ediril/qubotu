import os
from pathlib import Path

HOME = Path(os.environ.get("QUBOTU_HOME", Path.home() / ".qubotu"))
DB_PATH = HOME / "qubotu.db"
AGENTS_DIR = HOME / "agents"
MODELS_DIR = HOME / "models"
BROWSER_DIR = HOME / "browser"
LOG_PATH = HOME / "daemon.log"

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


def ensure_dirs() -> None:
    for d in (HOME, AGENTS_DIR, MODELS_DIR, BROWSER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Conversations, memory, and an API key live here: the owner only.
    HOME.chmod(0o700)
