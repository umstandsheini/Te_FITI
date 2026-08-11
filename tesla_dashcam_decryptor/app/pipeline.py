"""
Decrypts files for which a FEK is already present in the key store.
NO Tesla contact. Keys are NEVER deleted here (they live in the keystore).

Per file:
  1) Decrypt eCryptfs locally (AES-128-CBC) + ftyp validation
  2) Optionally embed FEK into decrypted MP4 (uuid box, ignored by players)
  3) SEI telemetry -> <name>.telemetry.json
  4) Optionally delete encrypted original (key remains in store!)
"""
import os, json, base64, struct, subprocess
from ecryptfs import EcryptfsFile
from telemetry import extract_telemetry
import keybridge


def _grab_frame(src_mp4, tmp, seek, width):
    cmd = ["ffmpeg", "-nostdin", "-y", "-ss", f"{max(0.0, seek):.2f}", "-i", src_mp4,
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    return r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0, r


def make_thumbnail(src_mp4, out_jpg, seek=1.0, width=240, fallback_seek=None):
    """Extract one frame from a (decrypted/plain) mp4 as JPG -> out_jpg (ffmpeg).

    fallback_seek: if the primary seek yields no frame (e.g. an event offset
    that lands past the end of a short clip), retry here so the clip still
    gets *a* thumbnail rather than none. 0.0 is always tried last regardless
    of fallback_seek: the ordinary (non-event) case seeks at a fixed 1.0s, and
    the trailing segment of a drive is often under a second long, so without
    an unconditional near-zero attempt those clips silently had no fallback
    at all whenever fallback_seek happened to equal seek (both 1.0s)."""
    os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
    tmp = out_jpg + ".part"
    seeks = [max(0.0, seek)]
    for s in (fallback_seek, 0.0):
        if s is not None and all(abs(s - x) > 0.01 for x in seeks):
            seeks.append(max(0.0, s))
    try:
        r = None
        for i, s in enumerate(seeks):
            ok, r = _grab_frame(src_mp4, tmp, s, width)
            if ok:
                os.replace(tmp, out_jpg)
                return True
            if i < len(seeks) - 1:
                print(f"[thumb] seek {s:.2f}s gave no frame, retrying at {seeks[i+1]:.2f}s "
                      f"src={src_mp4}", flush=True)
        print(f"[thumb] ffmpeg rc={r.returncode} src={src_mp4} err={r.stderr[-300:]!r}", flush=True)
    except FileNotFoundError:
        print("[thumb] ffmpeg not found (not installed?)", flush=True)
    except Exception as e:
        print(f"[thumb] ffmpeg EXC {e} src={src_mp4}", flush=True)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False

# fixed UUID (16 B) for the embedded FEK box
_FEK_UUID = bytes.fromhex("54e5d0c0da5c4f1e9b3a7c0011223344")
_FEK_MAGIC = b"TDCFEK01"


def embed_fek(mp4: bytes, fek: bytes) -> bytes:
    payload = _FEK_MAGIC + fek
    box = struct.pack(">I", 8 + 16 + len(payload)) + b"uuid" + _FEK_UUID + payload
    return mp4 + box


def _write_tel(mp4: bytes, tel_path: str, source: str):
    tel = extract_telemetry(mp4)
    tel["source"] = source
    os.makedirs(os.path.dirname(tel_path) or ".", exist_ok=True)
    json.dump(tel, open(tel_path, "w", encoding="utf-8"), separators=(",", ":"))


def decrypt_and_cache(enc_path, out_path, fek, embed_key=False, with_telemetry=True):
    """Atomically decrypts ONE file -> out_path (+ optional telemetry JSON)."""
    data = open(enc_path, "rb").read()
    plain = EcryptfsFile(data).decrypt(fek)
    is_mp4 = enc_path.lower().endswith(".mp4")
    if is_mp4 and plain[4:8] != b"ftyp":
        raise ValueError("wrong FEK (no ftyp)")
    if is_mp4 and embed_key:
        plain = embed_fek(plain, fek)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(plain)
    os.replace(tmp, out_path)
    if with_telemetry and is_mp4:
        try:
            _write_tel(plain, os.path.splitext(out_path)[0] + ".telemetry.json",
                       os.path.basename(out_path))
        except Exception:
            pass
    return True


def telemetry_for_plain(src_path, tel_path):
    """Extract telemetry from an unencrypted mp4 (no decryption, read-only)."""
    if os.path.exists(tel_path):
        return
    data = open(src_path, "rb").read()
    _write_tel(data, tel_path, os.path.basename(src_path))


def retag_telemetry(mp4_path, tel_path):
    """Re-extract telemetry from an mp4 already on disk (decrypted or plain),
    overwriting the cached JSON in place. Unlike telemetry_for_plain, this
    deliberately does NOT skip when tel_path already exists — it exists to fix
    telemetry.json files written by an older, buggy extract_telemetry(). The
    mp4 itself is read-only input and is never modified."""
    data = open(mp4_path, "rb").read()
    _write_tel(data, tel_path, os.path.basename(mp4_path))


def decrypt_pending(src_dir, out_dir, keys, delete_originals=False,
                    embed_key=False, log=print):
    cand = keybridge.files_needing_decrypt(src_dir, out_dir, keys)
    done = errs = 0
    for src_path in cand:
        cid = keybridge.clip_id(src_dir, src_path)
        out_path = os.path.join(out_dir, cid)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        try:
            data = open(src_path, "rb").read()
            fek = base64.b64decode(keys[cid])
            plain = EcryptfsFile(data).decrypt(fek)
        except Exception as e:
            log(f"  [error] {cid}: {e}"); errs += 1; continue
        is_mp4 = cid.lower().endswith(".mp4")
        if is_mp4 and plain[4:8] != b"ftyp":
            log(f"  [error] {cid}: wrong FEK (no ftyp)"); errs += 1; continue
        if is_mp4 and embed_key:
            plain = embed_fek(plain, fek)
        tmp = out_path + ".part"
        with open(tmp, "wb") as f:
            f.write(plain)
        os.replace(tmp, out_path)
        done += 1
        if is_mp4:
            try:
                tel = extract_telemetry(plain)
                tel["source"] = os.path.basename(cid)
                json.dump(tel, open(os.path.splitext(out_path)[0] + ".telemetry.json",
                                    "w", encoding="utf-8"), separators=(",", ":"))
            except Exception as e:
                log(f"  [warn] telemetry {cid}: {e}")
        if delete_originals:   # key remains in the store
            try:
                os.remove(src_path)
            except OSError as e:
                log(f"  [warn] delete {cid}: {e}")
    return {"need_decrypt": len(cand), "decrypted": done, "errors": errs}
