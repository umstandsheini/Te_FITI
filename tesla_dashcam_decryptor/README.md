# Te_FITI – Tesla Fleet Integration, Telemetry & Infotainment

A **multi-camera dashcam viewer** for Home Assistant that plays Tesla
Dashcam and Sentry clips directly from your NAS — with a live **HUD overlay**
(speed, gear, steering, gas, brake, blinkers, autopilot) extracted from
in-stream telemetry, a **GPS map** and an **event info panel**.

**Encryption is optional.** If your Tesla encrypts clips (firmware 2026.20+,
`EncryptedClips`, eCryptfs format), Te_FITI can decrypt them — either
temporarily for viewing or permanently saved to your NAS. This requires a
**one-time login to your Tesla account** to fetch the per-file encryption
keys (FEKs). After that first contact the keys are stored locally and
decryption works fully offline, with no further Tesla communication needed.

If your car does not encrypt clips (or you disable it in
*Controls → Safety → Encrypt dashcam recordings*), Te_FITI works as a
pure local viewer without any Tesla account interaction at all.

## Features

- **Clip list first** — the default view is the searchable clip list, with
  **Map** and **Analytics** as secondary tabs
- **Map area filter** — an interactive map (GPS trip routes + event markers,
  grouped by location with a disambiguation popup when several clips share a
  spot). Draw a rectangle to select an area, see how many clips fall inside,
  and jump to that filtered list. The area filter, plus the Driving / Event /
  Honk checkboxes, all combine — with a live result count
- **Trips** — clips are grouped into drives (contiguous by a 20-minute gap
  threshold), with distance, clip count and event counts, and a floating
  trip card with prev/next navigation; "View clips" filters the list to that
  drive
- **Analytics tab** — storage usage per folder/vehicle, trip/clip
  statistics, and events-by-reason / clips-by-month charts. Clicking a row in
  the events chart filters the clip list to that reason
- **Trip detail viewer** — Analytics shows a map for any actual drive (0 km
  parked/Sentry clusters are left out of the picker) with real per-frame
  vehicle telemetry from the dashcam video itself (speed, not a
  position-derived estimate), colored by speed so slow and fast stretches are
  visible at a glance, plus a speed-over-time chart, event markers, and
  average/top speed + Autopilot-use + braking-event stats. "View clips" jumps
  to the Clips tab filtered to that trip. If a companion recorder (e.g.
  te_usbhub) syncs per-drive GPX tracks into a `Fahrten` folder, those fill in
  only the stretches with no video telemetry of their own (an undecryptable
  clip, or one not extracted yet) — GPX is a fallback source, never the
  primary one, and the viewer works fine without it
- **Filter by event reason** — a dropdown listing the reasons present in your
  library with their counts (object detection, honk, accelerometer, emergency
  braking, …), shown as a removable chip and combining with all other filters
- **Light/dark theme toggle**, persisted, respects your OS preference on
  first load
- **6-camera grid player** — front, rear, left, right, pillar L/R with
  synchronised playback, seek and speed control
- **Telemetry HUD** — speed, gear, steering wheel, accelerator bar, brake
  indicator, blinkers, autopilot status (extracted from H.264 SEI NALs). On
  an event clip, a colored badge next to it marks the exact moment the event
  happened as playback reaches it
- **GPS map** — live track from telemetry or single-point from event.json
- **"Nerd info" panel** — raw telemetry values + event metadata
  (trigger reason, location, camera)
- **Clip browser** — searchable, filterable by driving telemetry / event.
  If your `clips_subpath` contains vehicle folders (e.g. `Tesla1`,
  `Tesla2`), clips are automatically grouped per vehicle. Switch between a
  compact list and a grid of large thumbnails with the view toggle next to
  the result count. A multi-segment event collapses to its trigger clip by
  default, expandable to see every segment. RecentClips loop-recording
  (a drive, or a parked Sentry wake-up) collapses the same way — consecutive
  same-folder clips no more than 90s apart become one row, expandable
- **Cross-segment playback** — opening any clip from a collapsed event or
  RecentClips recording shows a strip of its other minutes; jump to any of
  them, step with prev/next, or just let playback continue — it auto-advances
  into the next segment instead of stopping, so it plays like one continuous
  recording
- **Per-camera fullscreen** — each video tile has a fullscreen button
- **Thumbnail grid** — auto-generated or from Tesla's thumb.png
- **Per-camera download** and full-clip ZIP export
- **Batch operations** — bulk key fetch, "Decrypt everything" (with a progress
  bar and a cancel button; deletes the encrypted originals afterwards if
  `delete_originals` is on, after confirming), bulk telemetry extraction, bulk
  thumbnail generation
- **Free up storage** — delete clips by category (no-event / a specific event
  reason / all, optionally only older than N days) to reclaim space, with a
  preview and confirmation. Mark clips you want to keep with 📌 in the player;
  protected clips are never deleted by this cleanup. "Keep telemetry data"
  (checked by default) leaves the extracted GPS/speed sidecar in place even
  though the video is deleted — it costs only a few KB per clip

## How Tesla stores clips

Tesla saves the rolling buffer as **one-minute segments**, so a single event
produces a folder with several clips — typically 4 for a Sentry trigger, up to
11 for a manual save that keeps the preceding ~10 minutes. All of them share one
`event.json`, which is why every segment of an event carries the 📅 badge. The
Analytics event counts are per *event*, not per segment.

## How encryption works

Tesla protects each file with an individual encryption key (FEK) bound to
your account and vehicle. The key can only be retrieved from
`dashcam.tesla.com` after authentication.

Te_FITI supports two ways to obtain the keys:

1. **Direct API (recommended):** Log in once via the viewer's 🔑 panel.
   Te_FITI stores a refresh token and fetches keys automatically for all
   new clips — one-time setup, then hands-off.
2. **Browser bookmarklet (fallback):** If the Direct API is blocked, a
   bookmarklet runs inside your logged-in `dashcam.tesla.com` session to
   download the keys as a JSON file, which you upload to the viewer.

Once fetched, FEKs are stored persistently next to the encrypted files on
your NAS (`.teslacam_keys.json`). From that point on, decryption is **fully
local and offline** — no further contact with Tesla is needed.

In the clip browser, encrypted clips are marked with a lock icon:
🔒 (green) = key available, ready to decrypt;
🔒 (grey) = no key yet, needs to be fetched first.

## Installation

1. **Settings → Add-ons → Add-on Store**
2. Top right **⋮ → Repositories**
3. Add: `https://github.com/bernd780/Te_FITI`
4. Reload the store → install **Te_FITI**

## Configuration

Only the first three options normally need changing. Everything else has a
working default.

### Connecting to your NAS

Te_FITI does not copy your footage anywhere — it mounts the share your Tesla
already writes to and reads it in place.

| Option | Default | Description |
|---|---|---|
| `smb_host` | — | IP address of your NAS, e.g. `192.168.1.100`. Use the IP rather than a hostname; name resolution inside add-on containers is not guaranteed. |
| `smb_share` | `Tesla_Video` | The share name only, **not** a path. For `\\192.168.1.100\Tesla_Video`, this is `Tesla_Video`. |
| `clips_subpath` | `TeslaCam` | Where the TeslaCam tree sits *inside* the share. This is the folder that contains `RecentClips`, `SavedClips` and `SentryClips`. Leave empty if they sit at the root of the share. |
| `smb_username` | *(empty)* | Leave both credentials empty to mount as guest. |
| `smb_password` | *(empty)* | Stored in the Home Assistant add-on options; it is passed to the mount and never leaves your machine. |
| `smb_domain` | *(empty)* | Only for Windows/AD shares that require a domain. |
| `smb_version` | `3.0` | Lower this only if the mount fails — old NAS firmware may need `2.1`. Avoid `1.0` unless you have no choice; SMBv1 is insecure. |

If the add-on fails to start, the mount is almost always the cause. The log
prints the exact CIFS error from `dmesg`, which usually names the problem
(wrong password, unknown share, unsupported protocol version).

### Vehicle folders

If `clips_subpath` contains one folder per car (e.g. `TeslaCam/Tesla1`,
`TeslaCam/Tesla2`), the viewer groups clips per vehicle automatically. No
configuration needed — it keys off the folder name starting with `Tesla`.

### Decryption

Relevant only if your car encrypts clips (firmware 2026.20+). On a car that
does not, these options do nothing.

| Option | Default | Description |
|---|---|---|
| `enable_direct_api` | `true` | Fetch the per-file keys straight from Tesla after a one-time login in the 🔑 panel. Turn off to use the browser bookmarklet instead. |
| `auto_decrypt` | `true` | Decrypt clips in the background as soon as a key is available, instead of on demand when you open one. Costs disk space on the NAS but makes playback instant. |
| `delete_originals` | `true` | **Deletes the encrypted originals** after a clip decrypts successfully. Frees space, but there is no undo — the decrypted copy in `dec_subpath` becomes your only copy. The key itself is kept either way. An original is only removed once the decrypt succeeded *and* the decrypted file exists and is non-empty. Applies both to the background decryption and to the "Decrypt everything" button. |
| `key_after_decrypt` | `hidden` | `hidden` keeps keys in the key store only. `embed` also writes the key into an ignored `uuid` box inside the decrypted MP4, so the file stays decryptable on its own — convenient, but anyone with the file then has its key. |
| `dec_subpath` | `decrypted` | Folder inside the share for decrypted clips, thumbnails and extracted telemetry. |
| `broken_subpath` | `broken` | Folder inside the share for clips that are encrypted but contain no key of their own, moved there by the "Move undecryptable clips aside" button. Must sit outside `clips_subpath`. Nothing is deleted — moving the folder back restores them. A separate "Delete permanently" button is always available for the same files (even without `broken_subpath` configured) and, unlike the move button, cannot be undone. |
| `trips_subpath` | `Fahrten` | Folder at the share root where a companion recorder (e.g. te_usbhub) syncs per-drive `.gpx` tracks. The trip detail viewer in Analytics works without this — it uses dashcam video telemetry as its primary source — but when this is set, GPX fills the gaps where no video telemetry exists (undecryptable/not-yet-extracted clips). Read-only — Te_FITI never writes here. |
| `enc_subpath` | *(empty)* | Legacy. Encrypted files are detected by their eCryptfs header wherever they are, so leave this empty unless you deliberately keep them in a separate folder. |

### Automatic background processing

Everything here runs on its own without visiting the Keys panel — each item below is just the equivalent of its manual button, run automatically. All are independently toggleable.

| Option | Default | Description |
|---|---|---|
| `auto_fetch_keys` | `true` | Fetch a key the moment a newly-discovered encrypted file without one shows up in a scan, instead of waiting for the next `interval_seconds` cycle. Only reacts to genuinely new files — a library with nothing new costs nothing extra. Requires `enable_direct_api`. |
| `auto_generate_thumbnails` | `true` | Same as the "Generate thumbnails" button, run automatically every cycle. |
| `auto_extract_telemetry` | `true` | Same as the "Extract all telemetry" button, run automatically every cycle. |
| `auto_purge_driving_older_than_days` | `0` (off) | **Deletes driving footage** (no Sentry/Saved event) once it's older than this many days — irreversible. `0` disables it. Event clips are never touched by this, only by an explicit manual purge. |
| `keep_telemetry_on_delete` | `true` | Whether the GPS/speed telemetry sidecar survives when a video is deleted — applies both here and to the manual purge dialog's "keep telemetry data" checkbox, which starts out matching this setting. With it on, trip history in Analytics survives even after the video itself is gone. |

### Behaviour and diagnostics

| Option | Default | Description |
|---|---|---|
| `interval_seconds` | `300` | How often the add-on looks for new clips, fetches missing keys, decrypts (`auto_decrypt`), and runs the automatic background jobs above. Lower means new clips are picked up sooner; it does not affect how fast the viewer loads. |
| `debug_logging` | `false` | Verbose log: per-request timing, scan duration split by phase, and cache hit/miss counts. Turn this on first when something is slow — it shows whether time goes into the directory walk, classifying new files, or reading metadata. |

### What the first start does

On first run — and after adding a large batch of clips — the add-on builds an
index of the share. It reads a 28-byte header from each new file to tell
encrypted clips from plain ones, and caches the answer in `/data`, so this
happens **once per file, ever**.

The viewer stays usable throughout: a progress bar shows which phase is
running, and the clip list appears by itself when the index is ready. On a
share with ~13,000 files expect a few minutes; later starts are immediate
because everything comes from the cache.

## Privacy

- All keys and decrypted videos stay on your own hardware.
- The viewer itself loads nothing from the internet: the map library ships
  inside the add-on image, so the interface works on a host with no internet
  access at all.
- External communication is limited to two things: the one-time key fetch from
  Tesla, and map tiles from CartoDB/OpenStreetMap — and the tiles are only
  requested once you actually open the Map tab.
- Intended only for your own vehicle recordings with your own Tesla account.
