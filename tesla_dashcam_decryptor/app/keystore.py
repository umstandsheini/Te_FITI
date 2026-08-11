"""
Persistent FEK store – stored as a hidden JSON file NEXT TO the video files
(in the encrypted tree on the NAS). Keys are accumulated here permanently;
once fetched = never ask Tesla again. NEVER deleted.

Format:  { "<clip_id>": "<base64-FEK>", ... }
clip_id = path of the file relative to the source root (e.g.
          "TeslaCam/EncryptedClips/RecentClips/2026-..-front.mp4").
"""
import os, json, threading

DEFAULT_NAME = ".teslacam_keys.json"
_lock = threading.Lock()


def default_path(src_dir: str) -> str:
    return os.path.join(src_dir, DEFAULT_NAME)


def load(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def save(path: str, keys: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, separators=(",", ":"))
    os.replace(tmp, path)


def merge(path: str, new_keys: dict) -> int:
    """Add new FEKs (existing ones are NOT overwritten/deleted).
    Returns the number of genuinely new keys."""
    with _lock:
        keys = load(path)
        n = 0
        for cid, key in (new_keys or {}).items():
            if cid and key and cid not in keys:
                keys[cid] = key
                n += 1
        if n:
            save(path, keys)
        return n
