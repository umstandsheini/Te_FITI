# Te_FITI – Tesla Dashcam & Sentry Mode viewer for Home Assistant

**Te_FITI** is a Home Assistant add-on that plays **Tesla Dashcam and Sentry Mode
clips** straight from your NAS: six-camera playback, a live **telemetry HUD**
(speed, gear, steering, brake, blinkers, Autopilot), a **GPS map**, trip
analysis and event metadata. It can **decrypt encrypted Tesla clips**
(firmware 2026.20+, `EncryptedClips`, eCryptfs) with a one-time Tesla login;
after that everything runs locally and offline.

> **Companion capture side:** [te_camhub](https://github.com/umstandsheini/te_camhub) (a teslausb fork) runs on a Raspberry Pi **in the car**: it emulates the USB drive, archives the clips to the NAS Te_FITI reads, and can place each clip's decryption key next to it there (sealed `*.key.json`, or raw `*.rawkey.json`) so Te_FITI decrypts without its own Tesla login. Hub = capture & archive in the car; Te_FITI = decrypt & view at home.

![Te_FITI Screenshot](docs/screenshot.png)

## What it does

- **View Tesla dashcam / Sentry footage from a NAS** (SMB share) in Home
  Assistant, all cameras synchronised, with per-camera fullscreen and download
- **Decrypt encrypted Tesla dashcam clips** (2026.20 and later): keys fetched
  once, stored locally, decrypted offline; single clip, batch or automatic
- **Telemetry HUD** extracted from the video itself (H.264 SEI): speed, gear,
  steering wheel, accelerator, brake, indicators, Autopilot
- **Trips and analytics**: drives grouped from clips, speed-coloured route map,
  speed chart, average and top speed, Autopilot share, braking events
- **Event browser**: Sentry and saved events by reason (honk, object detection,
  accelerometer, emergency braking), map area filter, event-moment marker
- **Storage tools**: free up space by category and age, keep telemetry when
  deleting videos, protect clips you want to keep
- **Privacy**: no cloud, no upload; the only Tesla contact is the one-time key fetch

Full feature list, configuration and all options:
[add-on documentation](tesla_dashcam_decryptor/README.md).

## Installation

1. **Settings → Add-ons → Add-on Store**
2. Top right **⋮ → Repositories**
3. Add this URL: `https://github.com/umstandsheini/Te_FITI`
4. Reload the store → install **Te_FITI**

## Findings about Tesla dashcam files

Things learned by reverse-engineering real footage, written up separately:
[Tesla dashcam findings](docs/TESLA-DASHCAM-FINDINGS.md)

- Encrypted clip format (header layout, per-page IV, wrapped key section)
- Why `event.json` and `thumb.png` in `EncryptedClips` cannot be read
- Sentry pre-buffer duplicates the same minutes in `RecentClips` and `SentryClips`
- How telemetry is embedded in the video, and how patchy it is

## Keywords

Tesla dashcam viewer, Tesla Sentry Mode viewer, TeslaCam, decrypt Tesla
dashcam 2026.20, EncryptedClips, eCryptfs, Home Assistant add-on, NAS,
teslausb, dashcam telemetry HUD, SEI telemetry, Tesla dashcam GPS map.
