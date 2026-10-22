import os
from pathlib import Path

def data_dir() -> Path:
    env = os.environ.get("MESSAGE_HOURS_DATA")
    if env:
        base = Path(env)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / "Message Hours"
    else:
        base = Path.home() / ".message-hours"
    base.mkdir(parents=True, exist_ok=True)
    return base
