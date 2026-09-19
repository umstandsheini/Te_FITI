# Tesla dashcam findings: encrypted clips (2026.20+), event.json, telemetry

Notes from building [Te_FITI](../README.md), a Home Assistant add-on that views,
decrypts and analyses Tesla Dashcam / Sentry Mode footage from a NAS. Everything
here was observed on a real library (about 3,400 clips, firmware
2026.26.6.1 at the time of writing, September 2026). Where something is a
claim rather than something we verified, it says so.

## TeslaCam folder layout

| Folder | Contents |
|---|---|
| `RecentClips` | Rolling loop recording, one file set per minute. No `event.json`. |
| `SavedClips` | Manually saved events (dashcam icon, honk). One folder per event, up to about 11 one-minute segments (it keeps the preceding ~10 minutes). |
| `SentryClips` | Sentry Mode events. One folder per event, typically 4 one-minute segments. |
| `EncryptedClips/…` | Since firmware 2026.20: the same three folders, but every file is an encrypted container. |

- Each minute is written per camera: `YYYY-MM-DD_HH-MM-SS-front.mp4`, `-back`,
  `-left_repeater`, `-right_repeater`, plus pillar cameras on some models.
- All segments of one event share one `event.json` (trigger reason, timestamp,
  rough location, camera index) and one `thumb.png`.
- **Duplicates by design:** a Sentry or Saved event contains a pre-buffer that
  repeats minutes already present in `RecentClips`. The same timestamp therefore
  exists twice, in two folders. Anything that lists or counts clips must
  de-duplicate by timestamp.

## Encrypted clip format (firmware 2026.20+)

Each video is an eCryptfs-style container with a per-file AES-128 key (FEK).

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Plaintext size, uint64 big-endian |
| 8 / 12 | 4 + 4 | Two magic words; `magic1 XOR magic2 == 0x3C81B7F5` |
| 16 | 4 | Version / flags (`0x03000002`) |
| 20 | 4 | Page size, 4096 |
| 4096 | 4 | `key_id`, uint32 |
| 4100 | 65 | Uncompressed EC public key (starts with `0x04`) |
| 4165 | 17 | VIN, ASCII |
| 4182 | 8 | Timestamp, uint64 |
| 4190 | 44 | Wrapped key |
| 8192 | … | Payload in 4096-byte pages, AES-128-CBC |

- **IV per page:** `rootIV = MD5(FEK)`; `IV = MD5(rootIV ‖ ASCII(page number)`
  zero-padded to 32 bytes`)[:16]`.
- Decrypt all pages, then cut the result to the plaintext size from the first
  8 bytes. A valid result has `ftyp` at byte 4.
- The 44-byte wrapped key is the request you send to Tesla; the FEK comes back
  unwrapped. Keys are bound to your account and vehicle.
- Independent write-up of the same scheme:
  [XGxF3/tesla-dashcam-decrypt](https://github.com/XGxF3/tesla-dashcam-decrypt).

**Practical consequence:** you need Tesla once per file (a one-time login on
`dashcam.tesla.com`, or the API behind it). After the FEK is stored locally,
decryption is fully offline. Te_FITI keeps FEKs in a local key store and never
asks again. It also accepts an already-unwrapped key placed next to a clip as
`<video>.mp4.rawkey.json` (field `fek_b64`), a convention used by a companion
hub, so a key that is already on disk is never requested from Tesla twice.

## event.json and thumb.png in encrypted folders

Observed on 2026-09-18 across the whole library:

- In encrypted Saved and Sentry folders, 216 of 216 clips (Aug 151, Sep 65)
  have no readable event data. In plain (unencrypted) event folders it is
  2,154 of 2,154 clips that do. The files are present on disk (a companion
  hub confirmed 110 of 110 encrypted event folders contain both).
- One such `event.json` was inspected in detail. It is 12,288 bytes: a valid container header (magic words correct),
  plaintext size 189 bytes (the size of a normal `event.json`), and **an
  all-zero key section at offset 4096**. There is nothing to send to Tesla.
- None of the 12,474 clip keys in the store decrypts one (checked: the result
  must parse as JSON). It is not the key of a sibling video.
- The raw bytes contain the ASCII string `_CONSOLE`. A
  second source (not verified by us) describes these files as encrypted with a
  console-local passphrase rather than a per-file key, which would fit an
  empty key section by design.

**Consequences:** for encrypted event clips the trigger reason (honk, sentry
object detection, accelerometer, emergency braking, …) and the exact event
second are not recoverable from the metadata. The video itself decrypts fine.
This is not lost data and not an archiving problem: the files exist and simply
cannot be read outside the vehicle. Older reports of "event.json missing on the
NAS" for encrypted folders were false negatives from tools that refuse to read
anything under `EncryptedClips/`.

## Telemetry embedded in the video

Tesla writes driving telemetry into the H.264 stream as SEI NAL units
(protobuf). Per frame: speed, gear, accelerator pedal, steering angle, brake,
turn signals, Autopilot state, latitude, longitude and heading.

- Extract by reading SEI NALs, removing emulation-prevention bytes and decoding
  the protobuf. Te_FITI does this in pure Python (`telemetry.py`).
- **Coverage is patchy.** In our library only about 15 percent of clips (609 of
  4,098) contain any telemetry, confirmed by re-parsing the original files.
- Telemetry can stop partway through a clip. Positions must be matched to the
  true video frame (VCL NAL index), not the SEI ordinal, or the overlay drifts
  by up to 18 seconds.

## Trips from clips

- Clips in the same vehicle that are no more than about 20 minutes apart form
  one drive; distance comes from the GPS track in the telemetry.
- When a clip has no telemetry, an external GPX track for the same time window
  can fill the gap. Never use it where video telemetry exists.
- Braking is inferred from the brake flag plus the speed drop per second; there
  is no deceleration sensor in the data.

## Related

- [Te_FITI README](../README.md): features, installation, configuration
- [Add-on documentation](../tesla_dashcam_decryptor/README.md)
- [Changelog](../tesla_dashcam_decryptor/CHANGELOG.md)
