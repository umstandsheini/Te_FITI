"""
SEI telemetry extractor (verified 1:1 against index.js).
Field names taken verbatim from the Tesla source code.
"""
import struct, hashlib

# Bump when the frame-timing model changes. server.py uses this to find
# already-decrypted clips whose cached telemetry.json predates a fix and
# needs re-extraction — it is written as the first key of the returned dict
# so a cheap partial read of the file is enough to check it, without parsing
# the whole (often 200+ KB) JSON. History: 2 = frame_idx is the sample's true
# position among H.264 VCL NALs (0.7.15 fix for telemetry lagging the video
# by up to 18s when SEI coverage stops partway through a clip); 1 (implicit,
# no marker) = frame_idx was the sample's ordinal among SEIs, which drifted
# from true elapsed time whenever coverage wasn't 1:1 with every video frame.
TELEMETRY_SCHEMA = 2

GEAR = {0: "P", 1: "D", 2: "R", 3: "N"}


def _be32(b, p): return (b[p] << 24) | (b[p+1] << 16) | (b[p+2] << 8) | b[p+3]


def _rm_ep(d):  # H.264 emulation-prevention 00 00 03 -> 00 00
    out = bytearray(); z = 0
    for i, x in enumerate(d):
        if z >= 2 and x == 3 and i+1 < len(d) and d[i+1] <= 3:
            z = 0; continue
        out.append(x); z = z+1 if x == 0 else 0
    return bytes(out)


def _varint(d, i):
    s = v = 0
    while True:
        c = d[i]; i += 1; v |= (c & 0x7f) << s
        if not c & 0x80: break
        s += 7
    return v, i


def _pb(d):
    f = {}; i = 0
    try:
        while i < len(d):
            tag, i = _varint(d, i); fn = tag >> 3; wt = tag & 7
            if wt == 0:   v, i = _varint(d, i); f[fn] = v
            elif wt == 5: f[fn] = struct.unpack('<f', d[i:i+4])[0]; i += 4
            elif wt == 1: f[fn] = struct.unpack('<d', d[i:i+8])[0]; i += 8
            elif wt == 2: ln, i = _varint(d, i); i += ln
            else: break
    except Exception:
        pass
    return f


def _fps(b, frame_count):
    """Video's true fps: frame_count / mvhd duration. frame_count must be the
    number of actual video (VCL) frames, not the number of telemetry SEIs —
    those can be sparser than the video itself (see extract_telemetry)."""
    p = 0; mp = ms = 0
    while p + 8 <= len(b):
        sz = _be32(b, p); t = b[p+4:p+8]
        if t == b'moov': mp, ms = p, sz
        if sz < 8: break
        p += sz
    mv = b.find(b'mvhd', mp, mp+ms) if ms else -1
    if mv < 0: return 36.0
    ver = b[mv+4]
    if ver == 1:
        ts = _be32(b, mv+8); dur = struct.unpack(">Q", b[mv+24:mv+32])[0]
    else:
        ts = _be32(b, mv+16); dur = _be32(b, mv+20)
    return (frame_count / (dur/ts)) if (ts and dur) else 36.0


def extract_telemetry(data: bytes) -> dict:
    """data = decrypted MP4 (bytes). Returns {fps, frame_count, frames:[...]}

    Tesla does not embed a telemetry SEI in every coded video frame — once the
    car stops (park, computer going to sleep, ...) the SEIs simply stop while
    the video keeps recording for a while longer. On a real clip only ~70% of
    the 2154 video frames carried an SEI, all of them contiguous from frame 0.

    Each SEI is therefore tagged with vcl_idx: its position among H.264 VCL
    NALs (type 1 = non-IDR slice, type 5 = IDR slice), i.e. its true position
    in the video's own frame timeline — not its position among *other SEIs*.
    Using the SEI's own ordinal for "t" (as this used to) implicitly assumes
    one SEI per video frame with no gaps; averaging (SEI count)/(video
    duration) into a synthetic fps then stretches whatever SEIs exist across
    the full duration. On the clip above that understated the true ~36 fps as
    ~25.3, so every timestamp ran fast relative to the video and grew a lag of
    9+ seconds by the middle of the clip and 18s by the point telemetry
    actually stopped — the HUD showed the car still rolling at 8-10 km/h while
    the frame on screen showed it already stopped.
    """
    p = mds = mde = 0
    while p + 8 <= len(data):
        sz = _be32(data, p); t = data[p+4:p+8]
        if t == b'mdat': mds, mde = p+8, p+sz
        if sz < 8: break
        p += sz
    raw = []
    vcl_idx = -1
    pos = mds
    while pos + 4 <= mde:
        ln = _be32(data, pos)
        if ln <= 0 or pos+4+ln > mde: break
        nal = data[pos+4:pos+4+ln]
        nal_type = (nal[0] & 0x1f) if nal else -1
        if nal_type == 6:
            r = _rm_ep(nal[1:]); j = 0
            while j < len(r):
                if r[j] == 0x80 and all(x == 0 for x in r[j+1:]): break
                pt = 0
                while j < len(r) and r[j] == 0xff: pt += 255; j += 1
                if j >= len(r): break
                pt += r[j]; j += 1; ps = 0
                while j < len(r) and r[j] == 0xff: ps += 255; j += 1
                if j >= len(r): break
                ps += r[j]; j += 1; pl = r[j:j+ps]; j += ps
                if pt == 5:
                    k = pl.find(b'\x08\x01')
                    if k >= 0:
                        # This SEI precedes the next VCL NAL in decode order,
                        # so it belongs to that (not-yet-seen) frame index.
                        raw.append((vcl_idx + 1, _pb(pl[k:])))
        elif nal_type in (1, 5):
            vcl_idx += 1
        pos += 4 + ln
    total_frames = vcl_idx + 1
    fps = _fps(data, total_frames) if total_frames else 36.0
    frames = []
    for frame_idx, f in raw:
        frames.append({
            "t": round(frame_idx / fps, 3),
            "speed_kmh": round(f.get(4, 0.0) * 3.6, 1),
            "gear": GEAR.get(f.get(2), str(f.get(2))),
            "accel": round(f[5], 1) if 5 in f else None,
            "steer": round(f[6], 1) if 6 in f else None,
            "brake": 9 in f,
            "blink_l": 7 in f,
            "blink_r": 8 in f,
            "autopilot": f.get(10, 0),
            "lat": round(f[11], 6) if 11 in f else None,
            "lon": round(f[12], 6) if 12 in f else None,
            "heading": round(f[13], 1) if 13 in f else None,
        })
    # frame_count is len(frames), not total_frames: the frontend clamps its
    # lookup index to frame_count-1, so once real playback time runs past the
    # last telemetry sample it holds on the last known value instead of
    # indexing past the end of a shorter array.
    # "schema" first so a 64-byte partial read is enough to check it later.
    return {"schema": TELEMETRY_SCHEMA, "fps": round(fps, 3),
            "frame_count": len(frames), "frames": frames}
