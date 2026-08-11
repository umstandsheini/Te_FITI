#!/usr/bin/env python3
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
"""
Te_FITI Viewer + hybrid orchestration (HA ingress, stdlib + ffmpeg only).

The viewer lists ALL clips under SCAN_ROOT (full TeslaCam tree). Per camera file:
  - plain   : never encrypted              -> directly playable
  - ready   : encrypted + in cache         -> directly playable
  - key     : encrypted + key available    -> decrypt on demand (prepare)
  - locked  : encrypted + NO key           -> fetch key first

Thumbnails: uses existing Tesla thumb.png; otherwise generates a frame via
ffmpeg at the event timestamp (event.json) or ~1 s and caches it.

  GET  /                      www/index.html
  GET  /api/status            counters + login + busy + last_api
  GET  /api/clips             ALL clips incl. camera states + has_tel
  GET  /api/thumb?id=         thumbnail (png/jpg), generated/cached
  GET  /api/event?id=         event.json data (seek offset, GPS, reason)
  POST /api/prepare           {id} -> decrypt clip on demand, return fresh clip
  POST /api/fetch             Direct API: fetch missing keys now
  POST /api/decrypt           decrypt all keyed clips now (batch)
  POST /api/decrypt/cancel    stop that batch after the files already in flight
  POST /api/telemetry_all     extract SEI telemetry for all plain clips missing it (batch)
  POST /api/telemetry_resync  re-extract telemetry for clips predating the frame-timing fix (batch)
  GET  /api/telemetry_resync/preview  count of clips telemetry_resync would touch
  POST /api/telemetry_all/cancel      stop backfill or resync after files already in flight
  GET  /api/cleanup/preview   count/bytes of already-decrypted originals delete_originals would remove
  POST /api/cleanup           remove them (batch)
  GET  /api/quarantine/preview  count/bytes of key-less files quarantine_broken would move
  POST /api/quarantine        move them to broken_subpath (batch)
  POST /api/quarantine/delete permanently delete them instead (batch; irreversible)
  GET  /api/purge/preview?category=&reason=&older_than_days=&keep_telemetry=  clips/bytes a purge would delete
  POST /api/purge             delete clips by category (batch; protected clips spared,
                               keep_telemetry=1 keeps the tiny GPS/speed sidecar files)
  POST /api/protect           {id, protected} -> mark/unmark a clip as keep-forever
  GET  /api/gpx_trips         GPX trips synced from te_usbhub (list + summary), if configured
  GET  /api/gpx?id=           one GPX trip's track points + summary for the viewer
  GET  /api/trips             clips grouped into trips (contiguous drives per vehicle)
  GET  /api/analytics         storage/clip/trip/event stats (cached 60s)
  POST /api/keys              FEKs (bookmarklet) -> store
  GET  /api/pending.json      items (without key) for the bookmarklet
  GET  /api/login/url         Direct API: login URL
  POST /api/login/exchange    Direct API: callback URL -> token
  GET  /api/zip?id=           clip (decrypted) as ZIP
  GET  /media/<scanrel>       file from cache OR plain (range-capable)
"""
import os, json, argparse, re, glob, posixpath, threading, time, base64, zipfile, hashlib, datetime, math
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import keybridge, pipeline, keystore, telemetry
from keybridge import is_ecryptfs
from tesla_auth import TeslaAuth
import tesla_api

WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = OUT_DIR = SCAN_DIR = "."   # SRC=enc root, OUT=cache, SCAN=full clip tree
BROKEN_DIR = ""                      # where undecryptable files are moved to
TRIPS_DIR = ""                       # GPX trips from te_usbhub (share root, optional)
ENC_PREFIX = "EncryptedClips"         # SRC_DIR relative to SCAN_DIR
KEYS_FILE = ""
INTERVAL = 300
DELETE = False
AUTO_DECRYPT = False
EMBED_KEY = False
DIRECT_API = True
DEBUG = False
LIST_TTL = 120
TRIP_GAP_MIN = 20     # minutes of inactivity that ends a trip
ANALYTICS_TTL = 300
ENC_WORKERS = 1       # parallel probes; >1 measured slower on SMB, see _classify_new
STATIC_CTYPES = {".css": "text/css", ".js": "application/javascript",
                 ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
                 ".woff2": "font/woff2", ".map": "application/json"}
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.+)\.mp4$", re.I)
CAM_NAMES = ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar")

auth = None
_lock = threading.Lock()
_busy = False
_last_api = {"ok": None, "msg": "", "got": 0}
_fetch_job = {"running": False, "done": 0, "total": 0}
_prep_locks = {}
_prep_guard = threading.Lock()
_lcache = {"t": 0.0, "data": None}
_lcache_guard = threading.Lock()
_thumb_job = {"running": False, "done": 0, "total": 0}
_thumb_guard = threading.Lock()
_tel_job = {"running": False, "done": 0, "total": 0, "mode": "", "skipped": 0}
_tel_guard = threading.Lock()
# Shared by the backfill (gen_all_telemetry) and resync (telemetry_resync_all)
# jobs, since they both write telemetry.json and running two at once would
# just have them race on the same files.
_tel_cancel = threading.Event()
_dec_job = {"running": False, "done": 0, "total": 0, "errors": 0, "deleting": False,
            "cancelled": False, "skipped": 0}
_dec_guard = threading.Lock()
# Set by /api/decrypt/cancel. Files already being decrypted finish — a clip is
# never left half-written — but nothing new is started.
_dec_cancel = threading.Event()
_analytics_cache = {"t": 0.0, "data": None}
_trips_cache = {"t": 0.0, "data": None}
# Guards both derived caches and the set of builds currently in flight.
_derived_guard = threading.Lock()
_derived_running = set()
_rescan_running = False
_rescan_guard = threading.Lock()
# Serialises _scan() itself. _rescan_async() is single-flight, but the scheduler
# and the batch jobs call _scan() directly; without this they would start a
# second full scan on top of a running one, and two scans fighting over the same
# SMB mount are far slower than one (measured: 7 files/s down to 1.25).
_scan_lock = threading.Lock()
# Progress of the running scan, polled by the UI via /api/status. "phase" is one
# of walk (listing folders), index (per-file state), meta (telemetry/event JSON).
_scan_job = {"running": False, "phase": "", "done": 0, "total": 0,
             "clips": 0, "new": 0, "started": 0.0, "took": 0.0}

def _dbg(msg):
    if DEBUG:
        print(f"[debug] {msg}", flush=True)


# ---------- Persistent caches (survive add-on restarts) ----------
# Every entry below is keyed by a write-once path or clip id. TeslaCam files are
# never modified in place — a clip either exists or is deleted — so a cached
# answer stays valid until _scan() prunes the vanished path or the derived data
# is explicitly invalidated. Without this, each scan re-read every file off the
# NAS: on a network mount that dominates the request time by orders of magnitude.
_meta_cache = {}     # clip id  -> telemetry/event metadata (has_tel, gps, reason)
_enc_cache = {}      # scanrel  -> bool, file carries an eCryptfs header
_track_cache = {}    # clip id  -> decimated GPS track for the trip map
_size_cache = {}     # scanrel  -> file size in bytes (analytics storage stats)
_nokey_cache = {}    # scanrel  -> True, encrypted but carries no wrapped key
_telsync_cache = {}  # telsr    -> True once confirmed on the current telemetry.TELEMETRY_SCHEMA
_protected_cache = {}  # clip id -> True, user-marked "keep", never purged by "Free up storage"
_CACHE_FILES = {}    # name -> (dict, path)

def _cache_init(data_dir):
    """Bind each cache dict to its JSON file and load what is already there."""
    for name, d in (("meta", _meta_cache), ("enc", _enc_cache),
                    ("track", _track_cache), ("size", _size_cache),
                    ("nokey", _nokey_cache), ("telsync", _telsync_cache),
                    ("protected", _protected_cache)):
        path = os.path.join(data_dir, f".{name}_cache.json")
        _CACHE_FILES[name] = (d, path)
        if os.path.isfile(path):
            try:
                d.update(json.load(open(path, encoding="utf-8")))
            except Exception:
                pass
    _dbg(f"caches loaded: meta={len(_meta_cache)} enc={len(_enc_cache)} "
         f"track={len(_track_cache)} size={len(_size_cache)}")

def _cache_save(*names):
    """Atomically persist the named caches (all of them when called bare)."""
    for name in (names or tuple(_CACHE_FILES)):
        ent = _CACHE_FILES.get(name)
        if not ent:
            continue
        d, path = ent
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass


# ---------- Path helpers (scanrel = path relative to SCAN_DIR, posix) ----------
def _norm(rel):
    return posixpath.normpath(rel).lstrip("/")

def is_enc_sr(sr):
    return bool(ENC_PREFIX) and (sr == ENC_PREFIX or sr.startswith(ENC_PREFIX + "/"))

def enc_id(sr):
    return sr[len(ENC_PREFIX) + 1:] if is_enc_sr(sr) else sr

def cache_abspath(sr):
    return os.path.normpath(os.path.join(OUT_DIR, sr))

def src_abspath(sr):
    return os.path.normpath(os.path.join(SCAN_DIR, sr))

def _sr_of_cam(folder, ts, cam):
    return (folder + "/" if folder else "") + f"{ts}-{cam}.mp4"

def _telsr(folder, ts):
    return (folder + "/" if folder else "") + f"{ts}-front.telemetry.json"


# ---------- Directory walk ----------
def _listdir_tree(root, report=False):
    """All file paths under root, relative + posix, from one os.scandir pass per
    directory. SMB returns a whole directory listing in a single round trip, so
    this costs ~1 network op per folder — as opposed to one os.path.exists() per
    file, which is what made the old scan crawl on a NAS.

    report=True publishes folder progress to _scan_job. The total is unknown
    while walking (that is what the walk is finding out), so the UI shows this
    phase as indeterminate and only counts folders done."""
    files = set()
    if not root or not os.path.isdir(root):
        return files
    stack = [("", root)]
    folders = 0
    while stack:
        rel, absdir = stack.pop()
        try:
            entries = list(os.scandir(absdir))
        except OSError:
            continue
        folders += 1
        if report:
            _scan_job["done"] = folders
            _scan_job["clips"] = len(files)
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((r, e.path))
                else:
                    files.add(r)
            except OSError:
                continue
    return files


# ---------- Encrypted file detection (persistently cached) ----------
def _read_enc_header(abspath):
    """Does this file carry an eCryptfs header? One 28-byte read over SMB.
    Returns None if the file could not be read, so a transient failure is not
    cached as a definitive "not encrypted"."""
    try:
        with open(abspath, "rb") as f:
            return is_ecryptfs(f.read(28))
    except Exception:
        return None


def _is_encrypted(abspath, sr):
    """Cached answer for a single file. Normally a dict hit — _classify_new()
    has already filled the cache for everything the current scan will ask
    about; this only reads from disk on the single-clip paths."""
    if sr in _enc_cache:
        return _enc_cache[sr]
    result = _read_enc_header(abspath)
    if result is None:
        return False        # unreadable right now; do not cache the guess
    _enc_cache[sr] = result
    return result


def _probe_clip(srs):
    """Encryption state of a clip, from the first of its files that can be read.
    Falls back through the other cameras so one unreadable file does not
    misclassify the whole clip. None if none of them could be read."""
    for sr in srs:
        val = _read_enc_header(src_abspath(sr))
        if val is not None:
            return val
    return None


def _classify_new(mp4s):
    """Determine the encryption state of clips not seen before.

    Samples ONE camera file per clip and applies the answer to all six. The
    car writes a clip's cameras in a single pass, so they always share an
    encryption state — and this is a 6x cut in NAS round trips, which is what
    actually decides how long a first index takes.

    Reading fewer files is the lever here, not reading them faster: measured on
    a real SMB share, 8 parallel readers came out *slower* (5.0 files/s) than a
    single one (8.3 files/s) — the mount serialises the requests and the extra
    concurrency only adds contention. ENC_WORKERS stays available for shares
    that do benefit, but the default is deliberately 1.
    """
    by_clip = {}
    for sr, m in mp4s:
        if sr not in _enc_cache:
            by_clip.setdefault(posixpath.dirname(sr) + "|" + m.group(1), []).append(sr)
    if not by_clip:
        return 0
    clips = sorted(by_clip)
    _scan_job.update({"phase": "index", "done": 0, "total": len(clips), "new": len(clips)})

    def probe(cid):
        srs = by_clip[cid]
        # prefer the front camera: it is the one that always exists
        srs.sort(key=lambda s: ("-front.mp4" not in s.lower(), s))
        return _probe_clip(srs)

    done = files = 0
    with ThreadPoolExecutor(max_workers=max(1, ENC_WORKERS)) as ex:
        for i, (cid, enc) in enumerate(zip(clips, ex.map(probe, clips)), 1):
            if enc is not None:
                for sr in by_clip[cid]:
                    _enc_cache[sr] = enc
                    files += 1
            done = i
            if not i % 10:
                _scan_job["done"] = i
            if not i % 200:
                # Checkpoint: a restart during a long first index must not throw
                # away everything classified so far.
                _cache_save("enc")
    _scan_job["done"] = done
    _dbg(f"_classify_new: {done} clips probed -> {files} files classified")
    return files

# ---------- Clip state ----------
def _cam_state(sr, keys, out_files=None, src_files=None):
    """out_files: pre-walked set of paths under OUT_DIR. When given, the
    'is it already decrypted?' test is a set lookup instead of an SMB stat.

    src_files: pre-walked set under SCAN_DIR. A file missing from it but present
    in out_files has had its encrypted original deleted (delete_originals) — the
    decrypted copy is the only one left and is served straight from the cache.
    """
    if src_files is not None and sr not in src_files:
        return {"state": "ready", "url": "media/" + sr}
    if not _is_encrypted(src_abspath(sr), sr):
        return {"state": "plain", "url": "media/" + sr}
    cached = sr in out_files if out_files is not None else os.path.exists(cache_abspath(sr))
    if cached:
        return {"state": "ready", "url": "media/" + sr}
    if sr in keys or enc_id(sr) in keys:
        return {"state": "key", "url": None}
    if sr in _nokey_cache or enc_id(sr) in _nokey_cache:
        # Encrypted, but the file carries no wrapped key, so there is nothing
        # to ask Tesla for. Distinct from "locked" (a key can still arrive).
        return {"state": "nokey", "url": None}
    return {"state": "locked", "url": None}

def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def _read_event_json(path, cache=None):
    """event.json for a folder. One file is shared by all of a clip's one-minute
    segments, so within a scan it is read once per folder rather than per clip."""
    if cache is not None and path in cache:
        return cache[path]
    try:
        ev = json.load(open(path, encoding="utf-8"))
    except Exception:
        ev = {}
    if cache is not None:
        cache[path] = ev
    return ev


def _compute_meta(c, out_files=None, src_files=None, ev_cache=None):
    """Expensive: reads telemetry + event JSON from disk. Only ever runs for
    clips missing from _meta_cache; the pre-walked sets spare it two SMB stats."""
    telsr = _telsr(c["folder"], c["timestamp"])
    telp = cache_abspath(telsr)
    ht = False
    gps_center = None
    has_tel_file = telsr in out_files if out_files is not None else os.path.isfile(telp)
    if has_tel_file:
        try:
            tel = json.load(open(telp, encoding="utf-8"))
            ht = tel.get("frame_count", 0) > 0
            gps_pts = [[f["lat"], f["lon"]] for f in tel.get("frames", []) if f.get("lat") and f.get("lon")]
            if gps_pts:
                avg_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                avg_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                gps_center = {"center_lat": avg_lat, "center_lon": avg_lon}
        except Exception:
            ht = False
    ejsr = (c["folder"] + "/" if c["folder"] else "") + "event.json"
    ejp = os.path.join(SCAN_DIR, c["folder"], "event.json")
    # Only the .mp4 files are ever removed by delete_originals, so event.json
    # stays in the source folder and this lookup keeps working.
    ej_listed = ejsr in src_files if src_files is not None else os.path.isfile(ejp)
    reason = None
    event_ts = None
    if ej_listed:
        try:
            ev = _read_event_json(ejp, ev_cache)
            reason = ev.get("reason") or None
            event_ts = ev.get("timestamp") or None
            if not gps_center:
                lat = float(ev.get("est_lat") or ev.get("lat") or 0)
                lon = float(ev.get("est_lon") or ev.get("lon") or 0)
                if lat and lon:
                    gps_center = {"center_lat": lat, "center_lon": lon}
        except Exception:
            pass
    # has_event means "we actually have usable event data", not merely "a
    # same-named file was seen in the directory listing". For every event.json
    # in an EncryptedClips folder observed so far, the file that justified
    # ej_listed had already vanished by the time it was opened here (or never
    # fully materialized) -- reason and event_ts both silently stayed None
    # while he/has_event stayed True from the listing check alone. That made
    # every segment show the 📅 badge with nothing behind it, and none of them
    # were ever eligible to become the 🎯 trigger (_mark_trigger_segments
    # requires event_ts) -- exactly the "highlight only on older clips, never
    # on new ones" symptom, since the plain (unencrypted) clips that make up
    # the older library never hit this. Requiring real content closes the gap
    # between what the list badge promises and what the event panel can show.
    he = bool(reason or event_ts)
    return {"has_tel": ht, "has_event": he, "gps_bounds": gps_center,
            "has_data": ht or he, "reason": reason, "event_ts": event_ts}

_REASON_VALUE_RE = re.compile(r"^(.*?)_(\d+(?:\.\d+)?)$")

def _split_reason(reason):
    """Tesla appends the measured magnitude to some trigger reasons, e.g.
    'sentry_aware_accel_0.469145'. Split it into (category, value) so events
    group into one bucket instead of one per distinct float — otherwise the
    events-by-reason chart shows a separate row per measurement, and the UI's
    label lookup (keyed on the bare 'sentry_aware_accel') never matches.

    Applied on read rather than stored in _meta_cache: adding a key there would
    trip the cache sentinel in _finalize() and force a full re-read of every
    clip's metadata off the NAS on the first request after updating.
    """
    if not reason:
        return reason, None
    m = _REASON_VALUE_RE.match(reason)
    if not m:
        return reason, None
    try:
        return m.group(1), float(m.group(2))
    except ValueError:
        return reason, None


_EVENT_RECHECK_WINDOW = datetime.timedelta(hours=2)


def _clip_is_recent(ts, window=_EVENT_RECHECK_WINDOW):
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return False
    return (datetime.datetime.now() - t) < window


def _finalize(c, out_files=None, src_files=None, ev_cache=None):
    sts = [cm["state"] for cm in c["cameras"].values()]
    c["needs_prepare"] = "key" in sts
    c["has_locked"] = "locked" in sts
    c["has_nokey"] = "nokey" in sts
    c["playable"] = any(s in ("plain", "ready") for s in sts)
    cid = c["id"]
    cached = _meta_cache.get(cid)
    # "event_ts" is the cache sentinel: entries written before it existed are
    # recomputed once so the trigger segment can be identified. Entries from
    # before has_event required actual reason/event_ts content (see
    # _compute_meta) are a second, one-time migration case: has_event=True
    # with neither field set could only happen under the old, looser check,
    # so this can never recur once recomputed — safe to always retry, not
    # just for recent clips, since already-cached old clips need the same fix.
    stale_ghost_event = bool(cached) and cached.get("has_event") \
        and not cached.get("reason") and not cached.get("event_ts")
    if cached is None or "event_ts" not in cached or stale_ghost_event:
        cached = _compute_meta(c, out_files, src_files, ev_cache)
        _meta_cache[cid] = cached
    elif not cached["has_event"] and _clip_is_recent(c["timestamp"]):
        # Tesla can still be writing event.json when a fresh clip is first
        # scanned (segments and event.json don't necessarily land together),
        # so a "no event" answer from that scan got cached permanently and
        # the 📅/🎯 badges never appeared once the file did show up — the
        # exact "older clips show it, new ones never do" symptom reported.
        # Cheap: out_files/src_files are already in hand for this scan, so
        # this is just a couple of set lookups unless event.json now exists.
        fresh = _compute_meta(c, out_files, src_files, ev_cache)
        if fresh["has_event"]:
            cached = fresh
            _meta_cache[cid] = cached
    c["has_tel"] = cached["has_tel"]
    c["has_event"] = cached["has_event"]
    c["gps_bounds"] = cached.get("gps_bounds")
    c["has_data"] = cached["has_data"]
    c["reason"], c["reason_value"] = _split_reason(cached.get("reason"))
    c["event_ts"] = cached.get("event_ts")
    c["protected"] = cid in _protected_cache
    return c


def _mark_trigger_segments(clips):
    """Flag the one segment of each event folder in which the trigger actually
    happened.

    Tesla writes one event.json per folder but the rolling buffer as one-minute
    segments, so every segment inherits the event and the list showed four (or
    eleven) identically flagged rows with no way to tell where the door handle
    was actually pulled. The trigger belongs to the last segment that starts at
    or before it — robust to segments being 60 or 61 s apart, and to the final
    segment being short.
    """
    by_folder = {}
    for c in clips:
        if c.get("has_event") and c.get("event_ts"):
            by_folder.setdefault(c["folder"], []).append(c)
    for group in by_folder.values():
        try:
            ev_dt = datetime.datetime.strptime(group[0]["event_ts"][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        best, best_off = None, None
        for c in group:
            try:
                start = datetime.datetime.strptime(c["timestamp"], "%Y-%m-%d_%H-%M-%S")
            except ValueError:
                continue
            off = (ev_dt - start).total_seconds()
            if off < 0:
                continue                      # segment starts after the trigger
            if best_off is None or off < best_off:
                best, best_off = c, off
        if best is not None and best_off is not None and best_off <= 3600:
            best["is_trigger"] = True
            best["event_at"] = round(best_off, 1)

def _clip_track(c):
    """Decimated GPS track (<=40 pts) for a clip, from its telemetry file.

    Kept in its own persistent cache rather than in _meta_cache: adding a key to
    the _meta_cache entries would trip the cache-sentinel check in _finalize()
    and force a synchronous re-read of every historical clip's telemetry/event
    JSON on the first request after deploy. Telemetry files are written once and
    never edited, so a cached track never goes stale — which turns /api/trips
    from "read every telemetry JSON off the NAS" into pure CPU work."""
    if not c.get("has_tel"):
        return []
    cid = c["id"]
    hit = _track_cache.get(cid)
    if hit is not None:
        return hit
    telp = cache_abspath(_telsr(c["folder"], c["timestamp"]))
    if not os.path.isfile(telp):
        return []
    try:
        tel = json.load(open(telp, encoding="utf-8"))
        pts = [[f["lat"], f["lon"]] for f in tel.get("frames", []) if f.get("lat") and f.get("lon")]
        step = max(1, len(pts) // 40)
        track = pts[::step] if pts else []
    except Exception:
        return []
    _track_cache[cid] = track
    return track


def _prune_caches(src_files, clip_ids):
    """Drop cache entries for files/clips that no longer exist (delete_originals,
    manual cleanup). Only walks the caches when something actually vanished."""
    dropped = 0
    if len(_enc_cache) > len(src_files):
        for sr in [k for k in _enc_cache if k not in src_files]:
            del _enc_cache[sr]
            dropped += 1
    if len(_size_cache) > len(src_files):
        for sr in [k for k in _size_cache if k not in src_files]:
            del _size_cache[sr]
            dropped += 1
    for cache in (_meta_cache, _track_cache):
        if len(cache) > len(clip_ids):
            for cid in [k for k in cache if k not in clip_ids]:
                del cache[cid]
                dropped += 1
    return dropped


def _scan(keys=None):
    with _scan_lock:
        return _scan_locked(keys)


def _scan_locked(keys=None):
    t0 = time.time()
    _scan_job.update({"running": True, "phase": "walk", "done": 0, "total": 0,
                      "clips": 0, "new": 0, "started": t0})
    try:
        if keys is None:
            keys = keystore.load(KEYS_FILE)
        # Two directory walks instead of thousands of individual stat/open calls.
        src_files = _listdir_tree(SCAN_DIR, report=True)
        out_files = (src_files if os.path.normpath(OUT_DIR) == os.path.normpath(SCAN_DIR)
                     else _listdir_tree(OUT_DIR))
        t_walk = time.time()

        # The clip list is the union of both trees. With delete_originals on, a
        # clip's encrypted source is removed once it is decrypted — building the
        # list from SCAN_DIR alone would make those clips vanish from the viewer
        # even though the playable copy still sits in OUT_DIR.
        candidates = set(src_files)
        orphans = set()
        if out_files is not src_files:
            orphans = {sr for sr in out_files
                       if sr.lower().endswith(".mp4") and sr not in src_files}
            candidates |= orphans
        mp4s = [(sr, m) for sr, m in
                ((sr, TS_RE.search(posixpath.basename(sr))) for sr in candidates) if m]
        # Classify unseen files up front; afterwards the loop below is pure dict
        # lookups and touches the NAS no further. Orphans need no header read —
        # their source is gone, so there is nothing left to classify.
        enc_misses = _classify_new([(sr, m) for sr, m in mp4s if sr not in orphans])
        t_enc = time.time()

        clips = {}
        for i, (sr, m) in enumerate(mp4s):
            ts, cam = m.group(1), m.group(2).lower()
            folder = posixpath.dirname(sr)
            ck = folder + "|" + ts
            top = folder.split("/")[0] if folder else ""
            vehicle = top if top.lower().startswith("tesla") and top.lower() not in ("teslacam",) else ""
            c = clips.setdefault(ck, {"id": ck, "folder": folder, "timestamp": ts,
                                      "source": top, "vehicle": vehicle,
                                      "cameras": {}, "telemetry": None})
            c["cameras"][cam] = _cam_state(sr, keys, out_files, src_files)
            if cam == "front":
                telsr = _telsr(folder, ts)
                if telsr in out_files:
                    c["telemetry"] = "media/" + telsr
        t_state = time.time()

        meta_misses = sum(1 for cid in clips
                          if cid not in _meta_cache or "reason" not in _meta_cache[cid])
        _scan_job.update({"phase": "meta", "done": 0, "total": len(clips),
                          "clips": len(clips), "new": meta_misses})
        out = []
        ev_cache = {}          # event.json read once per folder, not per segment
        for i, c in enumerate(clips.values()):
            out.append(_finalize(c, out_files, src_files, ev_cache))
            if not i % 25:
                _scan_job["done"] = i
        _mark_trigger_segments(out)
        # id as tie-breaker: src_files is a set, so equal timestamps in different
        # folders would otherwise reshuffle the list between scans
        out.sort(key=lambda x: (x["timestamp"], x["id"]), reverse=True)
        pruned = _prune_caches(src_files, clips)
        if enc_misses or meta_misses or pruned:
            # Only write when something actually changed — a periodic background
            # rescan must not rewrite megabytes of JSON to the SD card every time.
            _cache_save("enc", "meta")
        _dbg(f"_scan: {len(out)} clips (walk {t_walk-t0:.3f}s / {len(src_files)} src + "
             f"{len(out_files)} out files, classify {t_enc-t_walk:.3f}s for {enc_misses} "
             f"new files across {ENC_WORKERS} workers, state {t_state-t_enc:.3f}s, "
             f"{meta_misses} meta misses, {pruned} pruned, "
             f"finalize+save {time.time()-t_state:.3f}s, total {time.time()-t0:.3f}s)")
        return out
    finally:
        _scan_job.update({"running": False, "phase": "", "took": time.time() - t0})


def _rescan_async(warm_derived=False):
    """Refresh the clip list in the background, one scan at a time. Requests are
    never blocked by a scan — they get the previous list until this finishes.

    warm_derived: after the scan produces a non-empty list, kick off the trips
    and analytics builds too. Set only for the start-up warm-up, so those
    caches (and the persistent size/track caches they fill) are ready by the
    time the user opens the Map or Analytics tab, instead of building on the
    first click. Not set for routine stale-refreshes: those just expire the
    derived caches and let the next request rebuild them lazily — with the
    size/track caches already warm from start-up, that rebuild is cheap."""
    global _rescan_running
    with _rescan_guard:
        if _rescan_running:
            return
        _rescan_running = True

    def work():
        global _rescan_running
        try:
            data = _scan()
            with _lcache_guard:
                _lcache["data"] = data
                _lcache["t"] = time.time()
            # The clip list just changed underneath trips/analytics — without
            # this they keep serving whatever they were built from, which on a
            # cold start is an empty list and reads as "0 clips, 0 trips".
            _derived_expire()
            if warm_derived and data:
                # Build trips + analytics now, in the background, so the first
                # visit to those tabs is instant. _derived_cached guards
                # against double-builds, so this is safe alongside any request
                # that happens to land during the warm-up.
                print(f"[warmup] index ready ({len(data)} clips), "
                      f"pre-building trips and analytics", flush=True)
                trips_cached()
                analytics_cached()
                # Fetch keys for any clip locked at start-up right away, rather
                # than leaving it up to the scheduler's next tick (up to
                # interval_seconds later). After a restart with new footage that
                # delay looked like "key fetch isn't working": logged in, keys
                # missing, nothing happening. The scheduler's first tick fires
                # before this scan finishes, so without this the first real
                # fetch could be a full interval away.
                if DIRECT_API and any(info["state"] == "locked"
                                      for c in data for info in c["cameras"].values()):
                    print("[warmup] locked clips present, fetching keys now", flush=True)
                    bg(run_cycle, do_fetch=True, do_decrypt=AUTO_DECRYPT)
        except Exception as e:
            print("[scan]", e, flush=True)
        finally:
            with _rescan_guard:
                _rescan_running = False

    threading.Thread(target=work, daemon=True).start()


def clips_cached():
    """Stale-while-revalidate, and never blocking: always answers from memory.
    A stale list is served as-is while it refreshes in the background; a cold
    cache answers empty rather than holding the request open for the length of a
    full NAS scan — the UI renders _scan_job progress instead and reloads when
    the scan finishes. Blocking here is what made the whole viewer look hung."""
    with _lcache_guard:
        data = _lcache["data"]
        fresh = data is not None and time.time() - _lcache["t"] < LIST_TTL
    if not fresh:
        _rescan_async()
    return data if data is not None else []

def invalidate(clip_id=None):
    _lcache["t"] = 0.0
    _derived_expire()
    if clip_id:
        _meta_cache.pop(clip_id, None)
        _track_cache.pop(clip_id, None)
    # Deliberately does NOT kick off a rescan: make_thumb() invalidates once per
    # generated thumbnail, which during a batch job would chain scans back to
    # back. The next request picks the refresh up and is served stale meanwhile.


def _clip_cams(cid):
    folder, ts = cid.rsplit("|", 1) if "|" in cid else ("", cid)
    cams = {}
    for path in glob.glob(os.path.join(SCAN_DIR, folder, f"{ts}-*.mp4")):
        m = TS_RE.search(os.path.basename(path))
        if m:
            cams[m.group(2).lower()] = _sr_of_cam(folder, ts, m.group(2).lower())
    return folder, ts, cams

def _scan_one(cid, keys=None):
    if keys is None:
        keys = keystore.load(KEYS_FILE)
    folder, ts, cams = _clip_cams(cid)
    if not cams:
        return None
    c = {"id": cid, "folder": folder, "timestamp": ts,
         "source": folder.split("/")[0] if folder else "", "cameras": {}, "telemetry": None}
    for cam, sr in cams.items():
        c["cameras"][cam] = _cam_state(sr, keys)
    if os.path.exists(cache_abspath(_telsr(folder, ts))):
        c["telemetry"] = "media/" + _telsr(folder, ts)
    return _finalize(c)


def counts(clips):
    cams = [cm for c in clips for cm in c["cameras"].values()]
    return {
        "clips": len(clips),
        "encrypted": sum(1 for cm in cams if cm["state"] in ("ready", "key", "locked", "nokey")),
        "plain": sum(1 for cm in cams if cm["state"] == "plain"),
        "decrypted": sum(1 for cm in cams if cm["state"] == "ready"),
        "keyed": sum(1 for cm in cams if cm["state"] in ("ready", "key")),
        "need_keys": sum(1 for cm in cams if cm["state"] == "locked"),
        # Encrypted with no wrapped key in the file: unrecoverable, and counted
        # apart so they stop looking like keys that are merely still missing.
        "no_wrapped_key": sum(1 for cm in cams if cm["state"] == "nokey"),
        "need_decrypt": sum(1 for cm in cams if cm["state"] == "key"),
        "with_telemetry": sum(1 for c in clips if c.get("has_tel")),
        "with_data": sum(1 for c in clips if c.get("has_data")),
    }

def _trip_route_and_events(clips):
    route = []
    events = {}
    seen_folders = set()      # one event per folder, not per one-minute segment
    for c in clips:
        track = _clip_track(c)
        if track:
            route.extend(track)
        elif c.get("gps_bounds"):
            route.append([c["gps_bounds"]["center_lat"], c["gps_bounds"]["center_lon"]])
        if c.get("has_event") and c.get("reason") and c["folder"] not in seen_folders:
            seen_folders.add(c["folder"])
            events[c["reason"]] = events.get(c["reason"], 0) + 1
    return route, events

def _make_trip(vehicle, clips):
    route, events = _trip_route_and_events(clips)
    dist = sum(_haversine_km(*route[i], *route[i + 1]) for i in range(len(route) - 1))
    bounds = None
    if route:
        lats = [p[0] for p in route]
        lons = [p[1] for p in route]
        bounds = {"min_lat": min(lats), "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)}
    return {
        "id": vehicle + "|" + clips[0]["timestamp"],
        "vehicle": vehicle,
        "start": clips[0]["timestamp"],
        "end": clips[-1]["timestamp"],
        "clip_ids": [c["id"] for c in clips],
        "clip_count": len(clips),
        "distance_km": round(dist, 2),
        "route": route,
        "bounds": bounds,
        "events": events,
        "event_total": sum(events.values()),
    }

def build_trips(clips, gap_min=TRIP_GAP_MIN):
    """Group clips per vehicle into contiguous trips by start-timestamp gap. Newest first."""
    t0 = time.time()
    tracks_before = len(_track_cache)
    by_vehicle = {}
    for c in clips:
        by_vehicle.setdefault(c.get("vehicle") or "", []).append(c)
    trips = []
    for vehicle, vclips in by_vehicle.items():
        vclips.sort(key=lambda c: c["timestamp"])
        group = []
        prev_dt = None
        for c in vclips:
            dt = datetime.datetime.strptime(c["timestamp"], "%Y-%m-%d_%H-%M-%S")
            if group and prev_dt and (dt - prev_dt).total_seconds() > gap_min * 60:
                trips.append(_make_trip(vehicle, group))
                group = []
            group.append(c)
            prev_dt = dt
        if group:
            trips.append(_make_trip(vehicle, group))
    trips.sort(key=lambda t: t["start"], reverse=True)
    if len(_track_cache) != tracks_before:
        _cache_save("track")
    _dbg(f"build_trips: {len(trips)} trips from {len(clips)} clips in {time.time()-t0:.3f}s "
         f"({len(_track_cache)-tracks_before} tracks read from disk)")
    return trips


def _derived_cached(name, cache, ttl, build):
    """Stale-while-revalidate for data derived from the clip list, with the same
    contract as clips_cached(): a request is never blocked by a build.

    The first build of either is expensive on a large library — trips reads the
    telemetry JSON of every clip with GPS data, analytics stats every camera
    file — and both were measured in the *minutes* on a NAS-backed share while
    holding the request open. They now fill their caches in the background.

    Nothing is built until the index exists: computing from the empty list of a
    cold start is what used to poison these caches with zeros for a whole TTL.
    """
    now = time.time()
    with _derived_guard:
        data = cache["data"]
        if data is not None and now - cache["t"] < ttl:
            return data
        if _lcache["data"] is None or name in _derived_running:
            return data                      # no index yet, or already building
        _derived_running.add(name)

    def work():
        try:
            result = build()
            with _derived_guard:
                cache["data"] = result
                cache["t"] = time.time()
        except Exception as e:
            print(f"[{name}]", e, flush=True)
        finally:
            with _derived_guard:
                _derived_running.discard(name)

    threading.Thread(target=work, daemon=True).start()
    return data


def _derived_expire():
    """Mark trips/analytics as stale after the clip list changed. Keeps the last
    good values so they are still served while the refresh runs."""
    with _derived_guard:
        _trips_cache["t"] = 0.0
        _analytics_cache["t"] = 0.0


def trips_cached():
    return _derived_cached("trips", _trips_cache, LIST_TTL,
                           lambda: build_trips(clips_cached())) or []


def _file_size(sr):
    """Size of a clip file, cached to disk — clips are write-once, so the value
    never changes. Saves one SMB stat per camera per clip on every analytics run."""
    hit = _size_cache.get(sr)
    if hit is not None:
        return hit
    full = resolve_media(sr)
    size = os.path.getsize(full) if full else 0
    _size_cache[sr] = size
    return size


def _trips_for_analytics():
    """Trips from cache when they are current, otherwise computed right here.

    compute_analytics() only ever runs on a derived-cache background thread, so
    blocking is fine — and necessary: trips_cached() is non-blocking and answers
    empty while its own build is still running, which would freeze zeros into
    the trip statistics for a whole analytics TTL.
    """
    with _derived_guard:
        data = _trips_cache["data"]
        if data is not None and time.time() - _trips_cache["t"] < LIST_TTL:
            return data
    return build_trips(clips_cached())


def compute_analytics():
    t0 = time.time()
    clips = clips_cached()
    trips = _trips_for_analytics()
    sizes_before = len(_size_cache)
    by_folder = {}
    for c in clips:
        top = c["folder"].split("/")[0] if c["folder"] else "(root)"
        entry = by_folder.setdefault(top, {"folder": top, "bytes": 0, "clip_count": 0})
        entry["clip_count"] += 1
        for cam in c["cameras"]:
            entry["bytes"] += _file_size(_sr_of_cam(c["folder"], c["timestamp"], cam))
    if len(_size_cache) != sizes_before:
        _cache_save("size")
    events_by_reason = {}
    clips_by_month = {}
    # Count events, not clip segments. Tesla saves the rolling buffer as
    # one-minute segments in a single folder per event, with one event.json for
    # all of them — so counting per clip multiplied every event by the number of
    # minutes it recorded (measured on a real library: 2,154 segments for 525
    # actual events, a 4x overstatement).
    seen_event_folders = set()
    for c in clips:
        if c.get("has_event") and c.get("reason") and c["folder"] not in seen_event_folders:
            seen_event_folders.add(c["folder"])
            events_by_reason[c["reason"]] = events_by_reason.get(c["reason"], 0) + 1
        m = c["timestamp"][:7]
        clips_by_month[m] = clips_by_month.get(m, 0) + 1
    distances = [t["distance_km"] for t in trips if t["distance_km"] > 0]
    _dbg(f"compute_analytics: {len(clips)} clips, {len(trips)} trips, storage stat'd in {time.time()-t0:.3f}s total")
    return {
        "storage": {"by_folder": sorted(by_folder.values(), key=lambda x: x["folder"])},
        "clips": counts(clips),
        # "total" counts every cluster of clips, most of which are parked Sentry
        # sessions with no movement; the averages only make sense over the ones
        # that actually moved. Both denominators are reported so the panel can
        # say which is which — showing 410 next to a 6 km average invited the
        # reading "410 x 6 km".
        "trips": {
            "total": len(trips),
            "moving": len(distances),
            "stationary": len(trips) - len(distances),
            "total_distance_km": round(sum(distances), 1),
            "avg_distance_km": round(sum(distances) / len(distances), 1) if distances else 0,
            "longest_km": round(max(distances), 1) if distances else 0,
        },
        "events_by_reason": events_by_reason,
        "clips_by_month": [{"month": k, "count": v} for k, v in sorted(clips_by_month.items())],
        "pending": False,
    }

def _analytics_pending():
    """Shape the UI can render while the real numbers are still being built."""
    return {"storage": {"by_folder": []}, "clips": counts([]),
            "trips": {"total": 0, "total_distance_km": 0, "avg_distance_km": 0,
                      "longest_km": 0},
            "events_by_reason": {}, "clips_by_month": [], "pending": True}


def analytics_cached():
    return _derived_cached("analytics", _analytics_cache, ANALYTICS_TTL,
                           compute_analytics) or _analytics_pending()


def _get_event_data(cid):
    """Returns event.json data: seek offset, GPS, reason, etc."""
    folder, ts, _ = _clip_cams(cid)
    if not folder or not ts:
        return None
    ej = os.path.join(SCAN_DIR, folder, "event.json")
    if not os.path.isfile(ej):
        return None
    try:
        ev = json.load(open(ej, encoding="utf-8"))
        result = {}
        et = ev.get("timestamp", "")
        if et:
            cs = datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
            evt = datetime.datetime.strptime(et[:19], "%Y-%m-%dT%H:%M:%S")
            off = (evt - cs).total_seconds()
            if 0 <= off <= 3600:
                result["seek"] = off
        lat = float(ev.get("est_lat") or ev.get("lat") or 0)
        lon = float(ev.get("est_lon") or ev.get("lon") or 0)
        if lat and lon:
            result["lat"] = lat
            result["lon"] = lon
        if ev.get("reason"):
            result["reason"], val = _split_reason(ev["reason"])
            if val is not None:
                result["reason_value"] = val
        if ev.get("city"):
            result["city"] = ev["city"]
        if ev.get("street"):
            result["street"] = ev["street"]
        if ev.get("camera") is not None:
            result["camera"] = ev["camera"]
        return result if result else None
    except Exception:
        return None


# ---------- Decryption ----------
def _clip_lock(cid):
    with _prep_guard:
        l = _prep_locks.get(cid)
        if l is None:
            l = threading.Lock()
            _prep_locks[cid] = l
        return l

def _key_for(sr, keys):
    """Find the FEK for an encrypted file (try full sr, then enc_id)."""
    if sr in keys:
        return base64.b64decode(keys[sr])
    eid = enc_id(sr)
    if eid in keys:
        return base64.b64decode(keys[eid])
    return None

def _decrypt_cam(sr, keys, delete_original=False):
    fek = _key_for(sr, keys)
    if not fek:
        raise KeyError(f"no key for {sr}")
    src, dst = src_abspath(sr), cache_abspath(sr)
    pipeline.decrypt_and_cache(src, dst, fek, embed_key=EMBED_KEY)
    if not delete_original:
        return
    # Irreversible: only ever after decrypt_and_cache() returned without raising
    # (it validates the ftyp box and writes atomically), and only once the
    # output is actually on disk and non-empty. The key stays in the store.
    try:
        if not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
            print(f"[delete] skipped {sr}: decrypted output missing/empty", flush=True)
            return
        os.remove(src)
        _enc_cache.pop(sr, None)
        print(f"[delete] removed encrypted original {sr}", flush=True)
    except OSError as e:
        print(f"[delete] {sr}: {e}", flush=True)

def prepare_clip(cid):
    keys = keystore.load(KEYS_FILE)
    folder, ts, cams = _clip_cams(cid)
    if not cams:
        return {"ok": False, "error": "clip not found"}
    jobs = []
    for cam, sr in cams.items():
        if _is_encrypted(src_abspath(sr), sr):
            if not os.path.exists(cache_abspath(sr)) and _key_for(sr, keys):
                jobs.append(("dec", sr))
        elif cam == "front":
            jobs.append(("tel", sr))
    errs = []
    def do(job):
        kind, sr = job
        try:
            if kind == "dec":
                _decrypt_cam(sr, keys)
            else:
                telp = os.path.splitext(cache_abspath(sr))[0] + ".telemetry.json"
                pipeline.telemetry_for_plain(src_abspath(sr), telp)
        except Exception as e:
            errs.append(f"{os.path.basename(sr)}: {e}")
    with _clip_lock(cid):
        if jobs:
            with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
                list(ex.map(do, jobs))
    invalidate(cid)
    return {"ok": not errs, "errors": errs, "clip": _scan_one(cid, keys)}

def broken_candidates():
    """Encrypted files that carry no wrapped key — nothing can ever decrypt
    them. Taken from the index, so listing them costs no NAS access."""
    out = []
    for c in clips_cached():
        for cam, info in c["cameras"].items():
            if info["state"] == "nokey":
                out.append(_sr_of_cam(c["folder"], c["timestamp"], cam))
    return out


def quarantine_preview():
    """What a move would affect, and where it would go."""
    srs = broken_candidates()
    known = [_size_cache[sr] for sr in srs if sr in _size_cache]
    est = sum(known)
    if known and len(known) < len(srs):
        est = int(est / len(known) * len(srs))
    return {"files": len(srs), "bytes_estimate": est,
            "exact": bool(srs) and len(known) == len(srs),
            "target": BROKEN_DIR, "enabled": bool(BROKEN_DIR)}


def quarantine_broken():
    """Move the undecryptable files out of the clip tree, keeping their folder
    structure so they stay identifiable — and reversible, which is why this
    moves rather than deletes."""
    global _dec_job
    if not BROKEN_DIR:
        return {"error": "no target folder configured"}
    if _dec_job.get("running"):
        return {"skipped": "busy"}
    srs = broken_candidates()
    _dec_cancel.clear()
    _dec_job = {"running": True, "done": 0, "total": len(srs), "errors": 0,
                "deleting": False, "cancelled": False, "skipped": 0,
                "phase": "quarantine", "freed": 0}
    moved = size = 0
    try:
        for sr in srs:
            if _dec_cancel.is_set():
                with _dec_guard:
                    _dec_job["skipped"] += 1
                    _dec_job["done"] += 1
                continue
            src = src_abspath(sr)
            dst = os.path.normpath(os.path.join(BROKEN_DIR, sr))
            try:
                if not os.path.isfile(src):
                    raise OSError("source is gone")
                if os.path.exists(dst):
                    raise OSError("already in the target folder")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                size += os.path.getsize(src)
                # Same SMB mount, so this is a rename: instant, and no risk of
                # a half-copied file.
                os.replace(src, dst)
                _enc_cache.pop(sr, None)
                _nokey_cache.pop(sr, None)
                _size_cache.pop(sr, None)
                moved += 1
            except OSError as e:
                print(f"[quarantine] {sr}: {e}", flush=True)
                with _dec_guard:
                    _dec_job["errors"] += 1
            with _dec_guard:
                _dec_job["done"] += 1
                _dec_job["freed"] = size
        print(f"[quarantine] moved {moved}/{len(srs)} undecryptable files to {BROKEN_DIR}, "
              f"{_dec_job['errors']} errors, cancelled={_dec_cancel.is_set()}", flush=True)
    finally:
        _cache_save("enc", "nokey", "size")
        _dec_job["cancelled"] = _dec_cancel.is_set()
        _dec_job["running"] = False
        _dec_cancel.clear()
        invalidate()
    return {"moved": moved, "bytes": size, "errors": _dec_job["errors"],
            "target": BROKEN_DIR}


def delete_broken():
    """Permanently delete the undecryptable files (no wrapped key -- Tesla
    never stored one for these, so nothing can ever decrypt them: not this
    add-on, not dashcam.tesla.com). Same candidate list as quarantine_broken(),
    but this actually frees the space instead of just moving the files aside,
    and unlike quarantine it cannot be undone -- offered as a separate,
    explicitly-confirmed action rather than folded into the move button."""
    global _dec_job
    if _dec_job.get("running"):
        return {"skipped": "busy"}
    srs = broken_candidates()
    _dec_cancel.clear()
    _dec_job = {"running": True, "done": 0, "total": len(srs), "errors": 0,
                "deleting": True, "cancelled": False, "skipped": 0,
                "phase": "broken_delete", "freed": 0}
    deleted = freed = 0
    try:
        for sr in srs:
            if _dec_cancel.is_set():
                with _dec_guard:
                    _dec_job["skipped"] += 1
                    _dec_job["done"] += 1
                continue
            src = src_abspath(sr)
            try:
                if not os.path.isfile(src):
                    raise OSError("source is gone")
                sz = os.path.getsize(src)
                os.remove(src)
                freed += sz
                _enc_cache.pop(sr, None)
                _nokey_cache.pop(sr, None)
                _size_cache.pop(sr, None)
                deleted += 1
            except OSError as e:
                print(f"[broken-delete] {sr}: {e}", flush=True)
                with _dec_guard:
                    _dec_job["errors"] += 1
            with _dec_guard:
                _dec_job["done"] += 1
                _dec_job["freed"] = freed
        print(f"[broken-delete] deleted {deleted}/{len(srs)} undecryptable files, "
              f"{freed} bytes freed, {_dec_job['errors']} errors, "
              f"cancelled={_dec_cancel.is_set()}", flush=True)
    finally:
        _cache_save("enc", "nokey", "size")
        _dec_job["cancelled"] = _dec_cancel.is_set()
        _dec_job["running"] = False
        _dec_cancel.clear()
        invalidate()
    return {"deleted": deleted, "freed": freed, "errors": _dec_job["errors"],
            "cancelled": _dec_job["cancelled"]}


def cleanup_candidates():
    """Encrypted originals whose clip is already decrypted — the leftovers from
    every decrypt that ran while delete_originals was broken (it never deleted
    anything before 0.7.8), plus anything decrypted with the option off.

    'ready' means the decrypted copy exists; presence in _enc_cache means the
    encrypted source was still there at the last scan (orphans get pruned), so
    the candidate list costs no NAS access at all.
    """
    out = []
    for c in clips_cached():
        for cam, info in c["cameras"].items():
            if info["state"] != "ready":
                continue
            sr = _sr_of_cam(c["folder"], c["timestamp"], cam)
            if sr in _enc_cache:
                out.append(sr)
    return out


def cleanup_preview():
    """Count and rough size of what a cleanup would delete. The size comes from
    the analytics size cache (decrypted bytes; an eCryptfs original is the same
    plus an 8 KB header), so it is an estimate, not a measurement."""
    srs = cleanup_candidates()
    known = [_size_cache[sr] for sr in srs if sr in _size_cache]
    est = sum(known)
    if known and len(known) < len(srs):
        est = int(est / len(known) * len(srs))     # extrapolate from what we know
    return {"files": len(srs), "bytes_estimate": est, "exact": len(known) == len(srs)}


def cleanup_originals():
    """Delete the encrypted originals of already-decrypted clips."""
    global _dec_job
    if _dec_job.get("running"):
        return {"skipped": "busy"}
    srs = cleanup_candidates()
    _dec_cancel.clear()
    _dec_job = {"running": True, "done": 0, "total": len(srs), "errors": 0,
                "deleting": True, "cancelled": False, "skipped": 0,
                "phase": "cleanup", "freed": 0}
    freed = 0
    try:
        for sr in srs:
            if _dec_cancel.is_set():
                with _dec_guard:
                    _dec_job["skipped"] += 1
                    _dec_job["done"] += 1
                continue
            src, dst = src_abspath(sr), cache_abspath(sr)
            try:
                # Never delete unless the decrypted copy is verifiably there
                if not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
                    with _dec_guard:
                        _dec_job["errors"] += 1
                        _dec_job["done"] += 1
                    print(f"[cleanup] skipped {sr}: no usable decrypted copy", flush=True)
                    continue
                size = os.path.getsize(src)
                os.remove(src)
                _enc_cache.pop(sr, None)
                freed += size
            except OSError as e:
                print(f"[cleanup] {sr}: {e}", flush=True)
                with _dec_guard:
                    _dec_job["errors"] += 1
            with _dec_guard:
                _dec_job["done"] += 1
                _dec_job["freed"] = freed
        print(f"[cleanup] removed {_dec_job['done']-_dec_job['errors']-_dec_job['skipped']}"
              f"/{_dec_job['total']} originals, {freed} bytes freed, "
              f"{_dec_job['errors']} errors, cancelled={_dec_cancel.is_set()}", flush=True)
    finally:
        _cache_save("enc")
        _dec_job["cancelled"] = _dec_cancel.is_set()
        _dec_job["running"] = False
        _dec_cancel.clear()
        invalidate()
    return {"removed": _dec_job["done"] - _dec_job["errors"] - _dec_job["skipped"],
            "freed": freed, "errors": _dec_job["errors"]}


# ---------- Protection flag (keep a clip forever) ----------
def set_protected(cid, on):
    """Mark/unmark a clip as protected. Protected clips are never deleted by
    the storage purge below. Persisted so it survives restarts."""
    if on:
        _protected_cache[cid] = True
    else:
        _protected_cache.pop(cid, None)
    _cache_save("protected")
    # Protection is purely local metadata — patch the cached clip in place
    # rather than invalidating, which would force a full NAS rescan.
    with _lcache_guard:
        data = _lcache["data"]
        if data:
            for c in data:
                if c["id"] == cid:
                    c["protected"] = bool(on)
                    break
    return {"id": cid, "protected": bool(on)}


# ---------- Storage purge (delete clips by category) ----------
def _purge_match(c, spec, now):
    """Does clip c fall into the purge selection? Protected clips never do.

    spec keys (all optional, ANDed):
      category: "no_event" | "event" | "reason" | "all"   (default "all")
      reason:   event reason to match when category == "reason"
      older_than_days: only clips whose start is at least this many days old
    """
    if c.get("protected"):
        return False
    cat = spec.get("category") or "all"
    has_ev = bool(c.get("has_event"))
    if cat == "no_event" and has_ev:
        return False
    if cat == "event" and not has_ev:
        return False
    if cat == "reason" and (not has_ev or c.get("reason") != spec.get("reason")):
        return False
    days = spec.get("older_than_days") or 0
    if days > 0:
        try:
            start = datetime.datetime.strptime(c["timestamp"], "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            return False
        if (now - start).total_seconds() < days * 86400:
            return False
    return True


def purge_candidates(spec, now=None):
    now = now or datetime.datetime.now()
    return [c for c in clips_cached() if _purge_match(c, spec, now)]


def purge_preview(spec):
    cand = purge_candidates(spec)
    files = est = known = 0
    for c in cand:
        for cam in c["cameras"]:
            sr = _sr_of_cam(c["folder"], c["timestamp"], cam)
            files += 1
            s = _size_cache.get(sr)
            if s is not None:
                est += s; known += 1
    if known and known < files:
        est = int(est / known * files)      # extrapolate size for un-stat'd files
    # how many clips the filter hit but protection spared, for a clear message
    protected_hit = sum(1 for c in clips_cached()
                        if c.get("protected") and _purge_match({**c, "protected": False},
                                                               spec, datetime.datetime.now()))
    return {"clips": len(cand), "files": files, "bytes_estimate": est,
            "exact": bool(files) and known == files, "protected_excluded": protected_hit}


def _remove_clip_files(c, keep_telemetry=False):
    """Delete every file belonging to one clip: decrypted copies, encrypted
    originals (if still present), telemetry sidecar and thumbnail. Returns the
    number of bytes freed. Never called for a protected clip (filtered out).
    With keep_telemetry, the telemetry sidecar (GPS/speed track, negligible
    size) survives so trip history stays intact after the video is gone."""
    freed = 0
    folder, ts = c["folder"], c["timestamp"]
    paths = []
    for cam in c["cameras"]:
        sr = _sr_of_cam(folder, ts, cam)
        paths += [cache_abspath(sr), src_abspath(sr)]
        _enc_cache.pop(sr, None); _size_cache.pop(sr, None)
    if not keep_telemetry:
        telsr = _telsr(folder, ts)
        paths += [cache_abspath(telsr), os.path.splitext(cache_abspath(telsr))[0] + ".json"]
        _track_cache.pop(c["id"], None)
    event_at = c.get("event_at")
    event_cam = (event_at is not None and _event_camera(folder)) or "front"
    paths.append(_thumb_cache_path(c["id"], event_at, event_cam))
    paths.append(_thumb_cache_path(c["id"], event_at))  # pre-camera-aware key, if still around
    paths.append(_thumb_cache_path(c["id"], None))
    for p in paths:
        try:
            if os.path.isfile(p):
                freed += os.path.getsize(p)
                os.remove(p)
        except OSError as e:
            print(f"[purge] {p}: {e}", flush=True)
    _meta_cache.pop(c["id"], None); _track_cache.pop(c["id"], None)
    return freed


def purge_clips(spec):
    """Delete all clips matching spec (protected clips excluded). This is
    permanent: it removes the decrypted copies and any encrypted originals."""
    global _dec_job
    if _dec_job.get("running"):
        return {"skipped": "busy"}
    cand = purge_candidates(spec)
    keep_telemetry = bool(spec.get("keep_telemetry"))
    _dec_cancel.clear()
    _dec_job = {"running": True, "done": 0, "total": len(cand), "errors": 0,
                "deleting": True, "cancelled": False, "skipped": 0,
                "phase": "purge", "freed": 0}
    freed = 0
    try:
        for c in cand:
            if _dec_cancel.is_set():
                with _dec_guard:
                    _dec_job["skipped"] += 1; _dec_job["done"] += 1
                continue
            try:
                freed += _remove_clip_files(c, keep_telemetry=keep_telemetry)
            except Exception as e:
                print(f"[purge] {c['id']}: {e}", flush=True)
                with _dec_guard:
                    _dec_job["errors"] += 1
            with _dec_guard:
                _dec_job["done"] += 1
                _dec_job["freed"] = freed
        print(f"[purge] deleted {_dec_job['done']-_dec_job['skipped']}/{_dec_job['total']} "
              f"clips ({spec}), {freed} bytes freed, {_dec_job['errors']} errors, "
              f"cancelled={_dec_cancel.is_set()}", flush=True)
    finally:
        _cache_save("enc", "size", "meta")
        _dec_job["cancelled"] = _dec_cancel.is_set()
        _dec_job["running"] = False
        _dec_cancel.clear()
        invalidate()
    return {"deleted": _dec_job["done"] - _dec_job["skipped"], "freed": freed,
            "errors": _dec_job["errors"], "cancelled": _dec_job["cancelled"]}


def _purge_spec_from(src):
    """Build a purge spec from a query-string dict or a JSON body dict."""
    def one(k):
        v = src.get(k)
        return v[0] if isinstance(v, list) else v
    days = one("older_than_days")
    try:
        days = int(days) if days not in (None, "") else 0
    except (TypeError, ValueError):
        days = 0
    kt = one("keep_telemetry")
    return {"category": one("category") or "all",
            "reason": one("reason") or "",
            "older_than_days": days,
            "keep_telemetry": kt in (True, "1", "true", "True", 1)}


def ensure_all():
    """Decrypt every clip that has a key. Deletes the encrypted originals
    afterwards when delete_originals is on — see _decrypt_cam for the guards."""
    global _dec_job
    if _dec_job.get("running"):
        return {"skipped": "busy"}
    keys = keystore.load(KEYS_FILE)
    # Uses the cached list, not a fresh _scan(): this runs from the scheduler
    # every interval_seconds, and forcing a full NAS re-scan each time is what
    # made the index build crawl. A list up to LIST_TTL old only means a clip
    # gets decrypted one cycle later.
    jobs = [(_sr_of_cam(c["folder"], c["timestamp"], cam), c["id"])
            for c in clips_cached()
            for cam, info in c["cameras"].items() if info["state"] == "key"]
    _dec_cancel.clear()
    _dec_job = {"running": True, "done": 0, "total": len(jobs), "errors": 0,
                "deleting": DELETE, "cancelled": False, "skipped": 0,
                "phase": "decrypt", "freed": 0}
    touched = set()
    def do(item):
        sr, cid = item
        if _dec_cancel.is_set():
            # Cancelled: drain the remaining work items without touching the NAS
            with _dec_guard:
                _dec_job["skipped"] += 1
                _dec_job["done"] += 1
            return
        try:
            _decrypt_cam(sr, keys, delete_original=DELETE)
            touched.add(cid)
        except Exception as e:
            print(f"[decrypt] {sr}: {e}", flush=True)
            with _dec_guard:
                _dec_job["errors"] += 1
        with _dec_guard:
            _dec_job["done"] += 1
    try:
        if jobs:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(do, jobs))
            print(f"[decrypt] {_dec_job['done']-_dec_job['skipped']}/{_dec_job['total']} done, "
                  f"{_dec_job['errors']} errors, {_dec_job['skipped']} skipped, "
                  f"cancelled={_dec_cancel.is_set()}, delete_originals={DELETE}", flush=True)
    finally:
        # Only the clips actually decrypted this round need their metadata
        # recomputed — decrypting produces telemetry, which changes has_tel/
        # gps_bounds/reason for exactly those clips and nothing else. This
        # used to be an unconditional _meta_cache.clear()/_track_cache.clear()
        # (and an unconditional invalidate()) every single time, including
        # when there was nothing to decrypt. Since this runs from the
        # scheduler every interval_seconds regardless of whether auto_decrypt
        # actually found work, on a large library that wiped the whole
        # metadata cache every few minutes — often before a scan slow enough
        # to need several minutes to rebuild it had even finished, so the
        # cache could never stay warm.
        for cid in touched:
            _meta_cache.pop(cid, None)
            _track_cache.pop(cid, None)
        _dec_job["cancelled"] = _dec_cancel.is_set()
        _dec_job["running"] = False
        _dec_cancel.clear()
        if touched:
            invalidate()
    return {"decrypted": _dec_job["done"] - _dec_job["errors"] - _dec_job["skipped"],
            "errors": _dec_job["errors"], "cancelled": _dec_job["cancelled"]}


# ---------- Thumbnails ----------
def _event_seek(folder, ts):
    """Fallback event offset (s) within THIS one clip, from the folder's
    event.json. Used only when the clip isn't in the cached index yet; the
    primary source is the trigger-aware event_at computed during the scan.

    The window is one segment (<60s), not 120s: an event folder holds several
    one-minute segments and the event lands in exactly one of them. A 120s
    window also matched the segment *before* the trigger and then seeked past
    that clip's end — producing a wrong or failed thumbnail."""
    ej = os.path.join(SCAN_DIR, folder, "event.json")
    if not os.path.isfile(ej):
        return None
    try:
        et = json.load(open(ej, encoding="utf-8")).get("timestamp", "")
        cs = datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
        ev = datetime.datetime.strptime(et[:19], "%Y-%m-%dT%H:%M:%S")
        off = (ev - cs).total_seconds()
        return off if 0 <= off < 60 else None
    except Exception:
        return None


def _clip_event_at(cid):
    """Event offset for this clip's thumbnail, or None. Prefers the
    trigger-aware event_at from the scanned index (set only on the segment that
    actually contains the event); falls back to a per-segment event.json read
    for a clip not indexed yet."""
    data = _lcache["data"]
    if data is not None:
        for c in data:
            if c["id"] == cid:
                return c.get("event_at")   # None on non-trigger / no-event clips
    folder, ts = cid.rsplit("|", 1) if "|" in cid else ("", cid)
    return _event_seek(folder, ts)

def make_thumb(cid):
    """Returns path to a thumbnail (png/jpg) or None (e.g. locked)."""
    folder, ts = cid.rsplit("|", 1) if "|" in cid else ("", cid)
    event_at = _clip_event_at(cid)
    # The trigger segment's thumbnail should show what actually caused the
    # event (e.g. a door-handle pull is a pillar camera, not front) — resolve
    # it from event.json's numeric "camera" field. Every other clip (and any
    # trigger clip whose event camera can't be resolved) keeps using front.
    cam = (event_at is not None and _event_camera(folder)) or "front"
    cache = _thumb_cache_path(cid, event_at, cam)
    if os.path.isfile(cache):
        # Fast path: already generated. Everything below this point costs at
        # least one NAS round trip (_clip_cams' directory listing, the
        # thumb.png stat) — skipping straight to the cache hit is what makes
        # scrolling a large, already-thumbnailed library feel instant instead
        # of laggy, since this is the common case for almost every request.
        return cache
    _, _, cams = _clip_cams(cid)
    # Tesla's own thumb.png is a generic frame, not the event moment — so only
    # use it when this clip is NOT the event trigger; event clips get a frame
    # grabbed at the event offset instead. In an encrypted clip folder, Tesla's
    # thumb.png is itself eCryptfs-wrapped (no FEK of its own, so it can't be
    # decrypted here) — serving it raw showed up as a black/broken image.
    # is_enc_sr(folder) used to gate this, but under the default empty
    # ENC_PREFIX (encryption detected by header, not path) it is always False,
    # so the raw wrapped file was served for every non-trigger segment of every
    # encrypted clip. Check the actual header instead, same as everywhere else.
    if event_at is None:
        tp = os.path.join(SCAN_DIR, folder, "thumb.png")
        if os.path.isfile(tp) and not _is_encrypted(tp, folder + "/thumb.png"):
            return tp
    cam_sr = _sr_of_cam(folder, ts, cam)
    if not cams.get(cam) and not os.path.isfile(src_abspath(cam_sr)) \
            and not os.path.isfile(cache_abspath(cam_sr)):
        # The event camera's file isn't available for this clip (e.g. that
        # camera didn't record this segment) — fall back to front rather than
        # showing nothing.
        if cam == "front":
            return None
        cam = "front"
        cache = _thumb_cache_path(cid, event_at, cam)
        if os.path.isfile(cache):
            return cache
        cam_sr = _sr_of_cam(folder, ts, cam)
        if not cams.get(cam) and not os.path.isfile(src_abspath(cam_sr)) \
                and not os.path.isfile(cache_abspath(cam_sr)):
            return None
    keys = keystore.load(KEYS_FILE)
    with _clip_lock(cid):
        if os.path.isfile(cache):
            return cache
        # Resolve the source without relying on is_enc_sr: with the default
        # empty ENC_PREFIX (encrypted files auto-detected by header, not by a
        # path prefix) is_enc_sr is always False, so the old branch always read
        # the plain original — but with delete_originals on, an encrypted clip's
        # original is gone and only the decrypted copy in OUT_DIR remains. That
        # made every such clip's thumbnail 404. Prefer the decrypted/cached copy
        # whenever it exists; otherwise use a plain original, or decrypt an
        # encrypted one on demand if we hold its key.
        cp = cache_abspath(cam_sr)
        sp = src_abspath(cam_sr)
        if os.path.isfile(cp):
            src = cp                      # decrypted or plain-in-cache copy
        elif os.path.isfile(sp) and not _is_encrypted(sp, cam_sr):
            src = sp                      # plain original
        elif os.path.isfile(sp):
            # encrypted original still present -> decrypt on demand if keyed
            if _key_for(cam_sr, keys) is None:
                return None
            try:
                _decrypt_cam(cam_sr, keys)
                invalidate()
            except Exception:
                return None
            src = cp
        else:
            return None                   # neither copy on disk
        # Seek to the event moment for a trigger clip; a normal ~1s frame
        # otherwise. fallback_seek guarantees a thumbnail even if the event
        # offset lands past the end of a short final segment.
        seek = event_at if event_at is not None else 1.0
        return cache if pipeline.make_thumbnail(src, cache, seek=seek,
                                                fallback_seek=1.0) else None


#  event.json's numeric "camera" field -> physical camera. Tesla doesn't
# document this format, and it does NOT simply match CAM_NAMES/file-naming
# order — that was the original assumption here and it was wrong. Verified
# by pulling every camera's actual frame at the event offset for real
# clips and checking which one shows the reported trigger:
#   0 -> front            confirmed: user-initiated events (honk, dashcam
#                          button) and AEB all report 0, and object-detection
#                          frames at 0 show the relevant content
#   1 -> back              matches the sole fisheye-lens camera in the set
#   5 -> left_repeater     confirmed 3x independently: two
#                          sentry_locked_handle_pulled events show a hand/
#                          person AT the door exactly in this camera's frame
#                          (one is a child visibly reaching for the handle),
#                          and one object-detection event shows two people
#                          standing at the car only in this camera
#   6 -> right_repeater    by symmetry with 5, plus one supporting (not
#                          airtight) example
# 2/3/4 are unverified carry-over guesses -- never observed in real data
# checked so far (only 0, 1, 5, 6 have appeared), so left as a reasonable
# placeholder rather than invented on no evidence. Fix as real examples
# turn up, the same way 5 and 6 were fixed here.
EVENT_CAMERA_INDEX = ("front", "back", "left_repeater", "right_repeater",
                       "left_pillar", "left_repeater", "right_repeater")


def _event_camera(folder):
    """Which physical camera actually saw the trigger, from event.json's
    numeric 'camera' field (see EVENT_CAMERA_INDEX). None if event.json has
    no camera field, isn't readable, or the index is out of range — callers
    fall back to front in that case."""
    try:
        ev = _read_event_json(os.path.join(SCAN_DIR, folder, "event.json"))
        idx = int(ev.get("camera"))
        if 0 <= idx < len(EVENT_CAMERA_INDEX):
            return EVENT_CAMERA_INDEX[idx]
    except (TypeError, ValueError):
        pass
    return None


def _thumb_cache_path(cid, event_at=None, cam=None):
    """Cache path for a clip's thumbnail. When the clip is an event trigger the
    event offset is folded into the key, so:
      * ordinary clips keep their old key — existing thumbnails stay valid,
      * an event clip gets a distinct key and regenerates once at the event
        moment instead of reusing a stale 1s-in thumbnail.
    Change the offset (unlikely, event.json is fixed) and it regenerates again.
    cam: only folded in when it isn't front, so the common case (front, or no
    event) keeps the same key as before this parameter existed — thumbnails
    already generated from a non-front event camera get their own key rather
    than colliding with (and never overwriting) an old front-camera one."""
    key = cid if event_at is None else f"{cid}@{event_at:.1f}"
    if cam and cam != "front":
        key += f"#{cam}"
    return os.path.join(OUT_DIR, ".thumbs", hashlib.sha1(key.encode()).hexdigest()[:20] + ".jpg")

def gen_all_thumbs():
    """Batch: generate thumbnails for every clip (encrypted + plain), not just
    ones with event/telemetry data. Previously restricted to has_data clips,
    which left ordinary driving segments (the bulk of most libraries) to load
    lazily one at a time on first scroll — with each cold thumbnail costing a
    decrypt + ffmpeg seek, that made the clip list look like it was missing
    thumbnails for anyone who hadn't already scrolled past every row once."""
    global _thumb_job
    if _thumb_job.get("running"):
        return
    clips = clips_cached()
    targets = []
    for c in clips:
        # same key make_thumb uses, so an event clip whose thumbnail was made
        # before this change (plain 1s frame, old key, or the wrong camera)
        # counts as missing here and regenerates from the right source
        event_at = c.get("event_at")
        cam = (event_at is not None and _event_camera(c["folder"])) or "front"
        thumb_path = _thumb_cache_path(c["id"], event_at, cam)
        if not os.path.isfile(thumb_path):
            targets.append(c["id"])
    _thumb_job = {"running": True, "done": 0, "total": len(targets), "started": time.time()}
    def do(cid):
        try:
            make_thumb(cid)
        except Exception:
            pass
        with _thumb_guard:
            _thumb_job["done"] += 1
    try:
        if targets:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(do, targets))
            print(f"[thumbs] {_thumb_job['done']}/{_thumb_job['total']} thumbnails generated", flush=True)
    finally:
        invalidate()
        _thumb_job["running"] = False


def gen_all_telemetry():
    """Batch: extract SEI telemetry for all plain front-camera clips that don't have it cached yet."""
    global _tel_job
    if _tel_job.get("running"):
        return
    clips = clips_cached()
    targets = []
    for c in clips:
        front = c["cameras"].get("front")
        if front and front["state"] == "plain" and not c.get("has_tel"):
            targets.append((_sr_of_cam(c["folder"], c["timestamp"], "front"), c["id"]))
    _tel_cancel.clear()
    _tel_job = {"running": True, "done": 0, "total": len(targets), "mode": "backfill", "skipped": 0}
    touched = set()
    def do(item):
        sr, cid = item
        if _tel_cancel.is_set():
            with _tel_guard:
                _tel_job["skipped"] += 1; _tel_job["done"] += 1
            return
        try:
            telp = os.path.splitext(cache_abspath(sr))[0] + ".telemetry.json"
            pipeline.telemetry_for_plain(src_abspath(sr), telp)
            touched.add(cid)
        except Exception as e:
            print(f"[telemetry] {sr}: {e}", flush=True)
        with _tel_guard:
            _tel_job["done"] += 1
    try:
        if targets:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(do, targets))
            print(f"[telemetry] {_tel_job['done']-_tel_job['skipped']}/{_tel_job['total']} extracted, "
                  f"{_tel_job['skipped']} skipped, cancelled={_tel_cancel.is_set()}", flush=True)
    finally:
        # Capture before clearing — see telemetry_resync_all() for why.
        _tel_job["cancelled"] = _tel_cancel.is_set()
        # Only the clips that actually got a fresh telemetry.json need their
        # metadata recomputed — see ensure_all() for why a blanket clear here
        # is expensive on a large library.
        for cid in touched:
            _meta_cache.pop(cid, None)
            _track_cache.pop(cid, None)
        if touched:
            invalidate()
        _tel_job["running"] = False
        _tel_cancel.clear()


def _telemetry_up_to_date(telsr):
    """Cheap check: is this cached telemetry.json already on the current
    extraction schema? "schema" is written as the FIRST key by
    extract_telemetry(), so a 64-byte partial read is enough — no need to
    parse the whole file, which can be 200+ KB. A positive result is
    remembered forever (a file's schema cannot regress), so this is paid at
    most once per file, ever."""
    if _telsync_cache.get(telsr):
        return True
    try:
        with open(cache_abspath(telsr), "rb") as f:
            head = f.read(64)
    except OSError:
        return True  # can't read it -> not ours to fix, don't offer it
    marker = b'"schema":%d' % telemetry.TELEMETRY_SCHEMA
    up_to_date = marker in head
    if up_to_date:
        _telsync_cache[telsr] = True
    return up_to_date


def telemetry_resync_candidates():
    """Clips whose cached telemetry.json predates the frame-timing fix
    (0.7.15) and can be corrected in place from the already-decrypted/plain
    mp4 already on disk — no NAS write beyond overwriting that one JSON file,
    no re-decryption, no contact with Tesla."""
    out = []
    for c in clips_cached():
        front = c["cameras"].get("front")
        if not (front and c.get("has_tel")):
            continue
        telsr = _telsr(c["folder"], c["timestamp"])
        if not _telemetry_up_to_date(telsr):
            out.append((c["id"], _sr_of_cam(c["folder"], c["timestamp"], "front"), telsr))
    return out


def telemetry_resync_preview():
    return {"files": len(telemetry_resync_candidates()),
            "schema": telemetry.TELEMETRY_SCHEMA}


def telemetry_resync_all():
    """Re-extract telemetry for every clip whose cached JSON predates the
    frame-timing fix. Reads the mp4 that is already on disk (decrypted or
    plain) and overwrites only its telemetry.json sidecar — the clip itself,
    its encrypted original (if any) and the key store are never touched."""
    global _tel_job
    if _tel_job.get("running"):
        return {"skipped": "busy"}
    cand = telemetry_resync_candidates()
    _tel_cancel.clear()
    _tel_job = {"running": True, "done": 0, "total": len(cand), "mode": "resync",
                "skipped": 0, "errors": 0}
    def do(item):
        cid, front_sr, telsr = item
        if _tel_cancel.is_set():
            with _tel_guard:
                _tel_job["skipped"] += 1; _tel_job["done"] += 1
            return
        try:
            full = resolve_media(front_sr)
            if not full:
                raise FileNotFoundError(front_sr)
            pipeline.retag_telemetry(full, cache_abspath(telsr))
            _telsync_cache[telsr] = True
        except Exception as e:
            print(f"[telemetry-resync] {cid}: {e}", flush=True)
            with _tel_guard:
                _tel_job["errors"] += 1
        with _tel_guard:
            _tel_job["done"] += 1
        _meta_cache.pop(cid, None)
        _track_cache.pop(cid, None)
    try:
        if cand:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(do, cand))
            print(f"[telemetry-resync] {_tel_job['done']-_tel_job['errors']-_tel_job['skipped']}"
                  f"/{_tel_job['total']} fixed, {_tel_job['errors']} errors, "
                  f"{_tel_job['skipped']} skipped, cancelled={_tel_cancel.is_set()}", flush=True)
    finally:
        # Capture before clearing: clearing the Event here means
        # _tel_cancel.is_set() would read False again by the time the return
        # statement below runs, always reporting "not cancelled" even when it
        # was — the same mistake fixed in ensure_all()/quarantine_broken().
        _tel_job["cancelled"] = _tel_cancel.is_set()
        _cache_save("telsync")
        invalidate()
        _tel_job["running"] = False
        _tel_cancel.clear()
    return {"fixed": _tel_job["done"] - _tel_job["errors"] - _tel_job["skipped"],
            "errors": _tel_job["errors"], "cancelled": _tel_job["cancelled"]}


# ---------- Direct API ----------
def pending_key_items():
    """Wrapped-key items for every encrypted file that still has no key.

    Candidates come from the index, where 'locked' already means *encrypted and
    keyless*. keybridge.scan_items() instead globs the whole tree and reads an
    8 KB header from every media file missing from the key store — which is
    every plain clip too, since those are never in it. On a library with 8,596
    plain files that was ~8,900 SMB reads to find 273, repeated every cycle.

    Files already known to carry no wrapped key are skipped outright: nothing
    about them can change, so re-reading 8 KB from each on every cycle is pure
    waste.
    """
    files = []
    for c in clips_cached():
        for cam, info in c["cameras"].items():
            if info["state"] != "locked":
                continue
            sr = _sr_of_cam(c["folder"], c["timestamp"], cam)
            if sr in _nokey_cache:
                continue
            files.append((src_abspath(sr), enc_id(sr)))
    res = keybridge.items_for(files)
    if res["no_wrapped_key"]:
        # Remember them so the UI can say "cannot be recovered" rather than
        # "no key yet", and so they are never read again.
        for cid in res["no_wrapped_key"]:
            _nokey_cache[cid] = True
        _cache_save("nokey")
        print(f"[fetch] {len(res['no_wrapped_key'])} file(s) carry no wrapped key "
              f"(Tesla stored none) — these can never be decrypted", flush=True)
        # Their clip state changes from "locked" to "nokey", so the list has to
        # be rebuilt for the counters and badges to reflect it.
        invalidate()
    _dbg(f"pending_key_items: {len(files)} candidates -> {len(res['items'])} items, "
         f"{len(res['no_wrapped_key'])} without a wrapped key, "
         f"{len(res['unreadable'])} unreadable")
    return res["items"]


def api_fetch(items):
    """Fetch keys for `items` from Tesla in chunks of 30 (their batch limit).
    Updates _fetch_job as it goes so the UI can show real progress instead of
    a static "Fetching keys…" with no idea how far along it is."""
    global _last_api, _fetch_job
    if not DIRECT_API:
        return {"ok": False, "msg": "Direct API disabled"}
    token = auth.get_access_token()
    if not token:
        _last_api = {"ok": False, "msg": "not logged in", "got": 0}
        return _last_api
    got = 0
    _fetch_job = {"running": True, "done": 0, "total": len(items)}
    try:
        for i in range(0, len(items), 30):
            chunk = items[i:i + 30]
            try:
                res = tesla_api.fetch_keys(chunk, token)
            except tesla_api.DecryptApiError as e:
                _last_api = {"ok": False, "msg": f"API: {e} (Bookmarklet nutzen)", "got": got}
                return _last_api
            got += keystore.merge(KEYS_FILE, res)
            _fetch_job["done"] = min(len(items), i + 30)
        _last_api = {"ok": True, "msg": "ok", "got": got}
        if got:
            invalidate()
    finally:
        _fetch_job["running"] = False
    return _last_api

def run_cycle(do_fetch=True, do_decrypt=None):
    global _busy, _last_api, _fetch_job
    if do_decrypt is None:
        do_decrypt = AUTO_DECRYPT
    if not _lock.acquire(blocking=False):
        return {"skipped": "busy"}
    _busy = True
    try:
        if do_fetch and DIRECT_API:
            # Indeterminate ("total":0) until pending_key_items() below knows
            # the real count — without this the UI has nothing to show for the
            # (occasionally slow, header-reading) gathering step before the
            # fetch itself starts.
            _fetch_job = {"running": True, "done": 0, "total": 0}
            try:
                # Resolve the token first and report if it can't be had — the old
                # `and auth.get_access_token()` in the condition skipped the whole
                # block silently when a refresh failed, so a dead login looked
                # identical to "nothing to do": the panel kept saying "logged in"
                # while need_keys sat unchanged with no error anywhere.
                token = auth.get_access_token()
                if not token:
                    logged_in = auth.status().get("logged_in")
                    _last_api = {"ok": False, "got": 0,
                                 "msg": ("Tesla login expired — open the Keys panel and log in again"
                                         if logged_in else "not logged in")}
                    print(f"[fetch] skipped: {_last_api['msg']}", flush=True)
                else:
                    items = pending_key_items()
                    if items:
                        r = api_fetch(items)
                        print(f"[fetch] {len(items)} offen, +{r.get('got',0)} Keys ({r.get('msg')})", flush=True)
                    else:
                        # Nothing to ask for. Without this _last_api kept its initial
                        # {"ok": None} and the panel stayed on "Fetching keys…" — the
                        # very case this run is most likely to hit.
                        _last_api = {"ok": True, "got": 0,
                                     "msg": "nothing left to request a key for"}
            finally:
                _fetch_job["running"] = False
        elif do_fetch:
            # enable_direct_api=false (e.g. the DEV instance, by design): the
            # button above has no server-side effect at all, so without this
            # the panel just sits on "Starting…" forever with nothing to show
            # for it — _fetch_job/_last_api never get touched otherwise.
            _last_api = {"ok": False, "got": 0,
                         "msg": "Direct API is disabled in add-on options"}
        if do_decrypt:
            r = ensure_all()
            if r["decrypted"]:
                print(f"[decrypt] {r}", flush=True)
    finally:
        _busy = False
        _lock.release()

def bg(fn, *a, **k):
    threading.Thread(target=fn, args=a, kwargs=k, daemon=True).start()

def scheduler():
    while True:
        try:
            run_cycle()
        except Exception as e:
            print("[sched]", e, flush=True)
        time.sleep(max(30, INTERVAL))


# ---------- GPX trips (from te_usbhub blackbox, synced to the share) ----------
# te_usbhub records a per-drive GPS track and pushes it as <trip_id>.gpx into a
# folder at the share root, next to TeslaCam/ — NOT inside the scanned tree, so
# it is passed in separately via --trips. The files are write-once (a finished
# trip never changes), which is what makes caching a parsed summary safe.
_TRKPT_RE = re.compile(
    r'<trkpt[^>]*\blat="([-\d.]+)"[^>]*\blon="([-\d.]+)"'
    r'(?:[^>]*>\s*<time>([^<]+)</time>)?', re.I)
_GPXNAME_RE = re.compile(r"<name>([^<]*)</name>", re.I)
_gpx_cache = {}          # id -> {"mtime","size","name","points","distance_km","bounds","start","end"}
_gpx_guard = threading.Lock()


def _gpx_id_ok(tid):
    """A trip id is a bare filename stem — reject anything that could escape
    TRIPS_DIR (path separators, '..')."""
    return bool(tid) and tid == os.path.basename(tid) and tid not in (".", "..")


def _gpx_path(tid):
    return os.path.join(TRIPS_DIR, tid + ".gpx")


def _gpx_dt_seconds(t1, t2):
    """Seconds between two GPX <time> strings, or None if either is missing
    or unparseable (a malformed/legacy point should not crash the trip)."""
    if not t1 or not t2:
        return None
    try:
        d1 = datetime.datetime.fromisoformat(t1.replace("Z", "+00:00"))
        d2 = datetime.datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return (d2 - d1).total_seconds()
    except ValueError:
        return None


_GPX_MAX_SPEED_KMH = 300.0  # clamp: one bad GPS fix must not skew the whole color scale


def _derive_speeds_kmh(pts, times):
    """Instantaneous speed at each point, derived from consecutive positions
    and timestamps — te_usbhub's GPX carries lat/lon/time only, no speed of
    its own (confirmed against its writer, teslausb-fork/hub/app/blackbox.py).
    A point takes the speed of the segment leading into it; the first point
    borrows the first segment's speed so the track has no unstyled start."""
    n = len(pts)
    speeds = [0.0] * n
    for i in range(1, n):
        dt = _gpx_dt_seconds(times[i - 1], times[i])
        if not dt or dt <= 0:
            speeds[i] = speeds[i - 1]
            continue
        km = _haversine_km(*pts[i - 1], *pts[i])
        speeds[i] = min(km / (dt / 3600.0), _GPX_MAX_SPEED_KMH)
    if n > 1:
        speeds[0] = speeds[1]
    return speeds


def _parse_gpx(text):
    """Track points + derived summary from GPX text. Tolerant of the minimal
    subset te_usbhub emits (trk/trkseg/trkpt lat/lon/time, nothing else)."""
    pts, times = [], []
    for lat, lon, t in _TRKPT_RE.findall(text):
        try:
            pts.append([float(lat), float(lon)])
        except ValueError:
            continue
        times.append(t.strip() if t else None)
    dist = sum(_haversine_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
    speeds = _derive_speeds_kmh(pts, times)
    bounds = None
    if pts:
        lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
        bounds = {"min_lat": min(lats), "max_lat": max(lats),
                  "min_lon": min(lons), "max_lon": max(lons)}
    start = next((t for t in times if t), None)
    end = next((t for t in reversed(times) if t), None)
    dur_s = _gpx_dt_seconds(start, end)
    avg_speed = round(dist / (dur_s / 3600.0), 1) if dur_s else None
    nm = _GPXNAME_RE.search(text)
    return {"name": nm.group(1).strip() if nm else "",
            # [lat, lon, speed_kmh] triples so the map can color each segment
            "points": [[p[0], p[1], round(s, 1)] for p, s in zip(pts, speeds)],
            "point_count": len(pts),
            "distance_km": round(dist, 2), "bounds": bounds,
            "start": start, "end": end,
            "avg_speed_kmh": avg_speed,
            "max_speed_kmh": round(max(speeds), 1) if speeds else None}


def _gpx_summary(tid):
    """Cached per-trip summary (no track points). Re-parsed only if the file's
    mtime/size changed, which for a finished trip's GPX never happens."""
    path = _gpx_path(tid)
    try:
        stt = os.stat(path)
    except OSError:
        return None
    with _gpx_guard:
        c = _gpx_cache.get(tid)
        if c and c["mtime"] == stt.st_mtime and c["size"] == stt.st_size:
            return {k: c[k] for k in ("name", "point_count", "distance_km", "bounds",
                                      "start", "end", "avg_speed_kmh", "max_speed_kmh")}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    p = _parse_gpx(text)
    entry = {"mtime": stt.st_mtime, "size": stt.st_size,
             "name": p["name"], "point_count": p["point_count"],
             "distance_km": p["distance_km"], "bounds": p["bounds"],
             "start": p["start"], "end": p["end"],
             "avg_speed_kmh": p["avg_speed_kmh"], "max_speed_kmh": p["max_speed_kmh"]}
    with _gpx_guard:
        _gpx_cache[tid] = entry
    return {k: entry[k] for k in ("name", "point_count", "distance_km", "bounds",
                                  "start", "end", "avg_speed_kmh", "max_speed_kmh")}


def list_gpx_trips():
    """Every GPX trip on the share, newest first, with a light summary. Returns
    [] when the feature is unconfigured or the folder is absent/empty, so the
    UI simply hides the section."""
    if not TRIPS_DIR or not os.path.isdir(TRIPS_DIR):
        return []
    out = []
    try:
        entries = list(os.scandir(TRIPS_DIR))
    except OSError:
        return []
    for e in entries:
        if not e.name.lower().endswith(".gpx"):
            continue
        tid = e.name[:-4]
        s = _gpx_summary(tid)
        if s and s["point_count"] > 0:
            out.append({"id": tid, **s})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


def gpx_track(tid):
    """Full track (points + summary) for one trip, for the map viewer."""
    if not TRIPS_DIR or not _gpx_id_ok(tid):
        return None
    path = _gpx_path(tid)
    if not os.path.isfile(path):
        return None
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    p = _parse_gpx(text)
    return {"id": tid, "name": p["name"], "track": p["points"],
            "point_count": p["point_count"], "distance_km": p["distance_km"],
            "bounds": p["bounds"], "start": p["start"], "end": p["end"],
            "avg_speed_kmh": p["avg_speed_kmh"], "max_speed_kmh": p["max_speed_kmh"]}


# ---------- Media serving ----------
def resolve_media(sr):
    sr = _norm(sr)
    cp = cache_abspath(sr)
    if cp.startswith(os.path.normpath(OUT_DIR)) and os.path.isfile(cp):
        return cp
    if not is_enc_sr(sr):
        sp = src_abspath(sr)
        if sp.startswith(os.path.normpath(SCAN_DIR)) and os.path.isfile(sp):
            return sp
    return None


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _file(self, path, ctype, extra=None):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        f = open(path, "rb")
        try:
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
                f.seek(start)
                chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(chunk)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            f.close()

    def _qs(self, key):
        return parse_qs(urlparse(self.path).query).get(key, [""])[0]

    def do_GET(self):
        self._t0 = time.time()
        path = self.path.split("?")[0]
        _dbg(f"GET {self.path} - start")
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WWW, "index.html"), "text/html",
                              {"Cache-Control": "no-cache, no-store, must-revalidate"})
        if path.startswith("/static/"):
            # Sub-paths are allowed (leaflet.css pulls images/*.png relative to
            # itself), so the traversal guard is an explicit containment check
            # rather than the basename() flattening this used to rely on.
            rel = posixpath.normpath(path[len("/static/"):]).lstrip("/")
            fp = os.path.normpath(os.path.join(WWW, rel))
            if not fp.startswith(os.path.normpath(WWW) + os.sep):
                return self._send(403, {"error": "forbidden"})
            if not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            # Revalidate rather than cache blindly: a plain max-age would leave
            # browsers running the previous app.js for that long after an add-on
            # update. With an ETag the check costs one 304 and stays correct.
            st = os.stat(fp)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            ct = STATIC_CTYPES.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
            return self._file(fp, ct, {"ETag": etag, "Cache-Control": "no-cache"})
        if path == "/api/status":
            st = counts(clips_cached())
            st["busy"] = _busy
            st["auto_decrypt"] = AUTO_DECRYPT
            st["direct_api"] = DIRECT_API
            st["login"] = auth.status()
            st["last_api"] = _last_api
            st["thumb_job"] = _thumb_job
            st["tel_job"] = _tel_job
            st["dec_job"] = _dec_job
            st["fetch_job"] = _fetch_job
            st["delete_originals"] = DELETE
            st["scan_job"] = _scan_job
            # False until the first scan has produced a list — lets the UI tell
            # "still indexing" apart from "genuinely no clips on the share"
            st["ready"] = _lcache["data"] is not None
            with _derived_guard:
                st["building"] = {"trips": "trips" in _derived_running,
                                  "analytics": "analytics" in _derived_running}
            return self._send(200, st)
        if path == "/api/clips":
            return self._send(200, clips_cached())
        if path == "/api/thumb":
            t = make_thumb(self._qs("id"))
            if not t:
                return self._send(404, {"error": "no thumb"})
            ct = "image/png" if t.endswith(".png") else "image/jpeg"
            return self._file(t, ct, {"Cache-Control": "max-age=86400"})
        if path == "/api/event":
            cid = self._qs("id")
            data = _get_event_data(cid)
            if data is None:
                return self._send(404, {"error": "no event"})
            return self._send(200, data)
        if path == "/api/cleanup/preview":
            return self._send(200, cleanup_preview())
        if path == "/api/quarantine/preview":
            return self._send(200, quarantine_preview())
        if path == "/api/telemetry_resync/preview":
            return self._send(200, telemetry_resync_preview())
        if path == "/api/purge/preview":
            spec = _purge_spec_from(parse_qs(urlparse(self.path).query))
            return self._send(200, purge_preview(spec))
        if path == "/api/gpx_trips":
            return self._send(200, {"enabled": bool(TRIPS_DIR), "trips": list_gpx_trips()})
        if path == "/api/gpx":
            g = gpx_track(self._qs("id"))
            if g is None:
                return self._send(404, {"error": "no such trip"})
            return self._send(200, g)
        if path == "/api/trips":
            return self._send(200, trips_cached())
        if path == "/api/analytics":
            return self._send(200, analytics_cached())
        if path == "/api/pending.json":
            items = pending_key_items()
            return self._send(200, {"items": items}, "application/json",
                              {"Content-Disposition": 'attachment; filename="pending_items.json"'})
        if path == "/api/login/url":
            return self._send(200, {"url": auth.make_login_url()})
        if path == "/api/zip":
            cid = self._qs("id")
            clip = _scan_one(cid)
            if not clip:
                return self._send(404, {"error": "clip"})
            if clip.get("needs_prepare"):
                prepare_clip(cid)
                clip = _scan_one(cid)
            members = []
            for cam in clip["cameras"]:
                full = resolve_media(_sr_of_cam(clip["folder"], clip["timestamp"], cam))
                if full and full.endswith(".mp4"):
                    members.append((full, os.path.basename(full)))
            tel = resolve_media(_telsr(clip["folder"], clip["timestamp"]))
            if tel:
                members.append((tel, os.path.basename(tel)))
            if not members:
                return self._send(404, {"error": "nichts zum Packen"})
            tmp = os.path.join(OUT_DIR, ".dl_%s.zip" % hashlib.sha1(cid.encode()).hexdigest()[:12])
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                    for full, arc in members:
                        z.write(full, arc)
                self._file(tmp, "application/zip",
                           {"Content-Disposition": 'attachment; filename="%s.zip"' % clip["timestamp"]})
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return
        if path.startswith("/media/"):
            full = resolve_media(path[len("/media/"):])
            if not full:
                return self._send(404, {"error": "not found"})
            ct = ("video/mp4" if full.endswith(".mp4") else
                  "application/json" if full.endswith(".json") else
                  "image/png" if full.endswith(".png") else "application/octet-stream")
            return self._file(full, ct)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        self._t0 = time.time()
        path = self.path.split("?")[0]
        _dbg(f"POST {self.path} - start")
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if path == "/api/prepare":
            try:
                cid = json.loads(raw or b"{}").get("id", "")
            except Exception:
                return self._send(400, {"ok": False, "error": "bad json"})
            return self._send(200, prepare_clip(cid))
        if path == "/api/keys":
            try:
                norm = keybridge.normalize_results(json.loads(raw or b"{}"))
                stored = keystore.merge(KEYS_FILE, norm)
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if stored:
                invalidate()
            if AUTO_DECRYPT:
                bg(run_cycle, do_fetch=False, do_decrypt=True)
            return self._send(200, {"ok": True, "stored": stored})
        if path == "/api/fetch":
            bg(run_cycle, do_fetch=True, do_decrypt=False)
            return self._send(200, {"ok": True})
        if path == "/api/decrypt":
            bg(ensure_all)
            return self._send(200, {"ok": True})
        if path == "/api/decrypt/cancel":
            _dec_cancel.set()
            return self._send(200, {"ok": True, "cancelling": _dec_job.get("running", False)})
        if path == "/api/quarantine":
            if not BROKEN_DIR:
                return self._send(400, {"ok": False,
                                        "error": "broken_subpath is not set in the add-on options"})
            bg(quarantine_broken)
            return self._send(200, {"ok": True})
        if path == "/api/quarantine/delete":
            bg(delete_broken)
            return self._send(200, {"ok": True})
        if path == "/api/cleanup":
            if not DELETE:
                return self._send(400, {"ok": False,
                                        "error": "delete_originals is off in the add-on options"})
            bg(cleanup_originals)
            return self._send(200, {"ok": True})
        if path == "/api/protect":
            try:
                body = json.loads(raw or b"{}")
                cid = body.get("id", "")
            except Exception:
                return self._send(400, {"ok": False, "error": "bad json"})
            if not cid:
                return self._send(400, {"ok": False, "error": "no id"})
            return self._send(200, set_protected(cid, bool(body.get("protected", True))))
        if path == "/api/purge":
            try:
                spec = _purge_spec_from(json.loads(raw or b"{}"))
            except Exception:
                return self._send(400, {"ok": False, "error": "bad json"})
            bg(purge_clips, spec)
            return self._send(200, {"ok": True})
        if path == "/api/thumbs_all":
            bg(gen_all_thumbs)
            return self._send(200, {"ok": True})
        if path == "/api/telemetry_all":
            bg(gen_all_telemetry)
            return self._send(200, {"ok": True})
        if path == "/api/telemetry_resync":
            bg(telemetry_resync_all)
            return self._send(200, {"ok": True})
        if path == "/api/telemetry_all/cancel":
            _tel_cancel.set()
            return self._send(200, {"ok": True, "cancelling": _tel_job.get("running", False)})
        if path == "/api/login/exchange":
            try:
                tok = auth.exchange_code(json.loads(raw or b"{}").get("callback", ""))
                bg(run_cycle, do_fetch=True, do_decrypt=False)
                return self._send(200, {"ok": True, "refresh": bool(tok.get("refresh_token"))})
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
        return self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if DEBUG:
            dur = time.time() - getattr(self, "_t0", time.time())
            print(f"[http] {fmt % args} ({dur:.3f}s)", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=".")
    p.add_argument("--out", default=".")
    p.add_argument("--scan", default="")
    p.add_argument("--broken", default="", help="folder to move undecryptable clips into")
    p.add_argument("--trips", default="", help="folder of GPX trip files from te_usbhub (optional)")
    p.add_argument("--keys", default="")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--delete", action="store_true")
    p.add_argument("--no-auto-decrypt", action="store_true")
    p.add_argument("--embed-key", action="store_true")
    p.add_argument("--no-direct-api", action="store_true")
    p.add_argument("--debug", action="store_true", help="Verbose logging: request timing, scan/analytics duration, cache hit/miss counts")
    a = p.parse_args()
    SRC_DIR = os.path.abspath(a.src)
    OUT_DIR = os.path.abspath(a.out)
    SCAN_DIR = os.path.abspath(a.scan) if a.scan else SRC_DIR
    ENC_PREFIX = os.path.relpath(SRC_DIR, SCAN_DIR).replace("\\", "/")
    if ENC_PREFIX in (".", ""):
        ENC_PREFIX = ""
    if a.broken:
        BROKEN_DIR = os.path.abspath(a.broken)
        # Must sit outside the scanned tree, or the moved files are simply
        # indexed again from their new location and nothing was gained.
        if BROKEN_DIR == SCAN_DIR or BROKEN_DIR.startswith(SCAN_DIR + os.sep):
            print(f"[config] broken_subpath '{a.broken}' is inside the scanned tree "
                  f"({SCAN_DIR}) — disabled, pick a folder next to it instead", flush=True)
            BROKEN_DIR = ""
    if a.trips:
        TRIPS_DIR = os.path.abspath(a.trips)
    KEYS_FILE = a.keys or keystore.default_path(SRC_DIR)
    INTERVAL = a.interval
    DELETE = a.delete
    AUTO_DECRYPT = not a.no_auto_decrypt
    EMBED_KEY = a.embed_key
    DIRECT_API = not a.no_direct_api
    DEBUG = a.debug
    auth = TeslaAuth(os.path.join(DATA_DIR, "token_store.json"))
    os.makedirs(os.path.join(OUT_DIR, ".thumbs"), exist_ok=True)
    _cache_init(DATA_DIR)
    # Warm the clip list before the panel is first opened, so the very first
    # request is served from memory instead of waiting for a full NAS scan.
    # warm_derived=True also pre-builds trips and analytics right after the
    # index is ready, so the Map and Analytics tabs are instant on first open
    # rather than kicking off a minutes-long build on click.
    _rescan_async(warm_derived=True)
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Viewer :{a.port} scan={SCAN_DIR} enc={SRC_DIR} (prefix='{ENC_PREFIX}') "
          f"out={OUT_DIR} broken={BROKEN_DIR or '-'} trips={TRIPS_DIR or '-'} "
          f"keys={KEYS_FILE} auto_decrypt={AUTO_DECRYPT} "
          f"embed={EMBED_KEY} direct_api={DIRECT_API} debug={DEBUG}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
