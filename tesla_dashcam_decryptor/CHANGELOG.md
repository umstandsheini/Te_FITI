# Changelog

## 0.7.37
- **RecentClips loop-recording now groups in the list, same as events.** A driving trip or a parked Sentry wake-up writes one clip per minute, and on a day with several of those the list turned into dozens of near-identical rows in a row — much worse than the event clutter 0.7.33 already fixed, since RecentClips has no shared event.json to key off. Consecutive same-folder clips no more than 90s apart (comfortably above the ~60s segment cadence, comfortably below the gap that means the car actually went back to sleep) now collapse into one "▸ N" row, same collapse/expand interaction as event groups. Opening any clip from inside a collapsed recording also gets the cross-segment player strip (prev/next, jump to any minute, auto-advance at the end) that was previously event-only — a multi-minute drive or a long Sentry wake-up now browses exactly like a multi-segment event does
- Verified against real DEV data: the 3-clip idle burst and 9-clip drive/wake-up blocks from today's library each collapsed correctly, expanding showed every segment, and jumping between segments in the player worked (verified via the 17:27–17:29 clips discussed with the user directly)

## 0.7.36
- **Fix: "Fetch all missing keys" got stuck on "Starting…" forever** whenever Direct API is disabled in add-on options (the DEV instance's permanent state, by design). `run_cycle()`'s key-fetch logic was gated entirely behind `do_fetch and DIRECT_API` with no `else` — so with Direct API off, the fetch status (`_last_api`) was never touched at all and stayed at its initial `{"ok": None}` forever. The status panel's own render logic explicitly skips updating its message while `ok` is `None` (to avoid clobbering it before the first real result), so neither the click nor the periodic 5s status poll ever moved the text off "Starting…" — not a timing race, a permanent dead end. `run_cycle()` now reports a clear "Direct API is disabled in add-on options" message in that case. The button's own click handler also now renders a completion message directly instead of relying entirely on the next status poll

## 0.7.35
- **New "Delete permanently" button for undecryptable clips.** The B2 panel previously only offered to move encrypted-but-keyless files aside (`broken_subpath`) — useful for getting them out of the clip list, but the files still sat on disk taking up space, and moving does nothing if `broken_subpath` isn't configured. There is no key to recover: Tesla never wrote one for these files, so neither this add-on nor dashcam.tesla.com can ever decrypt them. The new button frees the space outright. It's a separate, explicitly-confirmed, irreversible action — the existing move button is untouched and still available alongside it whenever `broken_subpath` is configured

## 0.7.34
- **A badge now marks the exact event moment during playback**, next to the HUD — appears as soon as the video crosses within ~1.5s of the event timestamp, colored/iconed by reason (same palette as the map's event markers), and disappears once you play past it. Shows immediately on opening a trigger clip too (it auto-seeks there already), not just once you hit play. Works independently of the telemetry HUD, so it still appears on an event clip with no per-frame telemetry to drive the HUD itself

## 0.7.33
- **Event clips no longer clutter the list.** A Sentry/Saved event's segments (all sharing one `event.json`) now collapse to a single row — the trigger clip, with a "▸ N" badge — instead of showing every 1-minute segment separately. Click the badge to expand and see (or collapse back) all of them, same idea as the existing per-vehicle grouping
- **New cross-event player navigation.** Opening any segment of an event shows a strip of its other minutes in the player, with prev/next buttons alongside it — jump to any segment without going back to the list. Reaching the end of a segment now auto-advances into the next one (and starts playing it) instead of just stopping, so a multi-segment event plays through like one continuous recording

## 0.7.32
- **Faster thumbnail loading while scrolling.** Every `/api/thumb` request did a NAS directory listing (`_clip_cams`' `glob.glob`) and a `thumb.png` stat *before* ever checking whether the thumbnail was already cached — on every single request, even the overwhelming majority that are cache hits once a library has been scrolled through once. Each of those was a real network round trip over the SMB mount. The cache check now happens first; the NAS calls only run on an actual cache miss. Measured on the real library (3829 clips): already-cached thumbnails went from ~186ms to ~42ms average per request (~4.5x) under the same concurrent-request pattern a browser uses while scrolling

## 0.7.31
- **Fix: grid view (0.7.30) rendered a blank/black clip list.** `.cliprow` had `overflow:hidden` (to round the card corners) while also being both a flex container and a CSS grid item — that combination forces a flex/grid item's automatic minimum size to 0 in every axis, and with no explicit height set, the whole card (thumbnail included) collapsed to 0px even though the row itself was present in the DOM. Reproduced and fixed live against the real DEV instance rather than guessed from a local repro. Corners are now rounded on the thumbnail/text parts directly instead of clipping the row itself, which sidesteps the interaction entirely

## 0.7.30
- **New grid view for the clip list**, toggled with a button next to the result count (persisted in your browser). The default list view keeps its compact 64×36 thumbnails; grid view lays clips out as cards filling the width of the page, each with a much bigger thumbnail — better for browsing by eye instead of by timestamp. Collapses gracefully down to a single column on narrow windows

## 0.7.29
- **Fix: the event-camera thumbnail (0.7.28) used the wrong camera-index mapping.** It carried over `CAM_NAMES`' file-naming order (0=front,1=back,2=left_repeater,3=right_repeater,4=left_pillar,5=right_pillar), which turned out not to match reality — that's an assumption, not something Tesla documents. Caught immediately: a reported clip's event.json said "Pillar R" (old index 5) but the actual movement was in `left_repeater`. Verified by pulling every camera's real frame at the event offset across several real clips: index 5 is `left_repeater` (confirmed 3x, including a child visibly reaching for the door handle on a `sentry_locked_handle_pulled` event), and index 6 — which real data uses constantly but the old 6-entry mapping couldn't even represent — is `right_repeater`. The "nerd info" panel's camera label is corrected to match, so the displayed text and the thumbnail now agree. 2/3/4 remain unverified guesses (never observed in real data checked so far) rather than newly invented numbers

## 0.7.28
- **The trigger segment's thumbnail now shows the camera that actually saw the event**, instead of always front. Resolved from event.json's numeric `camera` field (0=front, 1=back, 2=left_repeater, 3=right_repeater, 4=left_pillar, 5=right_pillar — same mapping the "nerd info" panel already used to label it). Confirmed against real events: several real Sentry triggers on this library were seen by the right pillar camera, not front, so their thumbnail was previously showing an angle that never captured the event at all. Falls back to front when the event camera didn't record that particular segment, or when the field is missing/out of range (a `camera: "6"` value shows up on real data with no corresponding file — handled the same as any other unavailable camera)

## 0.7.27
- **Fix: event clips only showed their trigger highlight/symbol on older clips, never on new ones.** Root-caused against real prod data: for every encrypted-clip event since the car started encrypting recordings, `event.json` was seen once in a directory listing (caching `has_event: true`) but was never actually readable when its content was read — and is still unreadable months later, so it is very likely not archived correctly for encrypted clips by the upstream recorder (documented separately, see `te_usbhub/EVENT_JSON_ENCRYPTED_CLIPS_ISSUE.md`). `has_event` now requires actually-usable data (a reason or timestamp), not just a same-named file having been listed once — so a clip no longer shows an event badge that leads nowhere, and existing library-wide cache entries in this broken state self-correct on the next scan, no manual reset needed
- **Hardening:** a clip whose `event.json` genuinely hasn't been written yet (Tesla can still be writing it when a brand-new clip is first scanned) is now rechecked for up to 2 hours after creation instead of caching a "no event" answer forever the moment it's first seen
- **GPX trip viewer now shows speed, colored on the map.** te_usbhub's GPX only carries position and time (confirmed against its writer), so speed is derived server-side from consecutive points' distance and elapsed time. The route is drawn as per-segment colored polylines (blue → green → yellow → red, fixed 0–150 km/h scale so different trips stay visually comparable) with a legend, and the trip info line now shows average and top speed alongside distance and duration

## 0.7.25
- **Fix: some thumbnails showed up solid black/broken.** Root cause confirmed against a real affected clip on prod: Tesla's own `thumb.png` inside an *encrypted* clip folder is itself eCryptfs-wrapped (its file size was an exact multiple of 4096 bytes past the 8192-byte header, and the header's magic field matched exactly) — it has no FEK of its own, so it can't be decrypted, and the server was serving the raw wrapped bytes straight out as `image/png`. The gate meant to skip `thumb.png` for encrypted folders (`not is_enc_sr(folder)`) never actually fired, for the same reason as the earlier missing-thumbnail bugs: `is_enc_sr` is always False under the default empty `ENC_PREFIX`, since encryption is detected by file header, not by path. It now checks the actual header before trusting `thumb.png`, and falls through to generating a real frame from the decrypted video when it's wrapped

## 0.7.24
- **Fix: thumbnails permanently missing for short trailing clips.** `make_thumbnail`'s retry only fired when `fallback_seek` differed from the primary `seek` — but the ordinary (non-event) case always calls it with `seek=1.0, fallback_seek=1.0`, the same value, so clips under ~1 second (typically the last RecentClips segment right before the car parks/Sentry stops) had no working fallback at all and 404'd forever. It now always tries a frame at 0.0s as a last resort regardless of what fallback_seek was. Confirmed against real prod data: 11 of 14 clips with a missing thumbnail were exactly this case
- **"Generate thumbnails" now covers every clip**, not just ones with event/telemetry data. It used to skip ordinary driving segments (often most of a library), leaving them to generate one at a time — slow, decrypt-then-ffmpeg — the first time each row scrolled into view, which looked like widespread missing thumbnails on any list that hadn't been fully scrolled through yet

## 0.7.23
- **"Fetch all missing keys" now shows real progress.** It used to just set the text to "Fetching keys…" and wait a fixed 4 seconds before refreshing — on a large batch the panel sat there with no indication anything was happening, and looked identical whether it was working or stuck. It now shows a progress bar (an indeterminate one while it gathers which files still need a key, then a determinate done/total once the Tesla request is underway) and disables the button until the fetch actually finishes. New `fetch_job` field in `/api/status`

## 0.7.22
- **"Free up storage" gains a "keep telemetry data" option** (checked by default): when purging clips, the extracted GPS/speed telemetry sidecar (a few KB per clip) is kept even though the video files themselves are deleted, since it costs negligible space. Uncheck it to delete the telemetry too, matching the previous behaviour. Applies to the `POST /api/purge` and `GET /api/purge/preview` endpoints via a new `keep_telemetry` parameter

## 0.7.21
- New **"Free up storage" section** (Keys panel, G): delete whole clips by category to reclaim space — clips without an event, clips with any event, clips with a specific event reason (e.g. door-handle), or all clips, optionally restricted to *older than N days*. Preview first (exact clip count + estimated size, and how many kept clips were spared), then a confirmation, a progress bar and cancel. Deletes the decrypted copies, any encrypted originals and the telemetry/thumbnail sidecars; the space reclaimed is reported. **Permanent, no undo**
- New **📌 Keep** flag: a "Keep" button in the player marks a clip as protected, shown as a 📌 badge in the clip list. Protected clips are **never** deleted by the storage cleanup. The flag is stored in `/data` (`.protected_cache.json`), survives restarts, and touches nothing on the NAS
- Endpoints: `GET /api/purge/preview`, `POST /api/purge`, `POST /api/protect`; the purge shares the single bulk-job slot and the cancel path with the other destructive operations

## 0.7.20
- **Fix: no thumbnails for decrypted clips whose encrypted original was deleted** (the common case with `delete_originals` on — which is why the newest clips at the top of the list showed nothing). `make_thumb` decided where to read the frame from via `is_enc_sr`, but with the default empty `ENC_PREFIX` (encrypted files are detected by header, not by a path prefix) that is always False, so it always tried the plain original in the source tree — which `delete_originals` had removed, leaving only the decrypted copy in `decrypted/`. The endpoint returned 404. The source is now resolved directly: the decrypted/cached copy is preferred when present, otherwise a plain original, otherwise an encrypted original is decrypted on demand if its key is held

## 0.7.19
- **Fix: a failed Tesla-token refresh silently skipped the key fetch.** `run_cycle` had `and auth.get_access_token()` in its condition, so when a refresh failed the whole fetch block was skipped with no message — the panel kept showing "logged in ✓" next to keys that never arrived, which looked exactly like "key fetch isn't working" with no clue why. It now reports a clear "Tesla login expired — open the Keys panel and log in again", and the login pill turns red and re-shows the login box when a fetch reports an expired session
- **Keys are now fetched immediately after start-up** for any clip locked at boot, instead of waiting up to `interval_seconds` for the scheduler's next tick. After a restart with new footage that delay was the actual symptom: logged in, keys missing, nothing visibly happening until the next cycle. The scheduler's first tick fires before the start-up scan finishes, so without this the first real fetch could be a full interval away

## 0.7.18
- **Event clips now show the event moment in their thumbnail** instead of a generic frame from the first second. The seek uses the trigger-aware `event_at` computed during the scan, so within a multi-segment event only the segment that actually contains the trigger seeks to it — the surrounding buffer segments keep a normal frame
- Fixes a latent bug in the old thumbnail seek: it allowed any offset up to 120s, which for the segment *before* the trigger seeked past that clip's own end and produced a wrong or failed thumbnail. The window is now one segment
- The event offset is folded into the thumbnail's cache key, so an event clip whose thumbnail was generated before this change (a plain 1s frame) regenerates once at the event moment, while ordinary clips keep their existing cached thumbnails untouched
- Thumbnail generation now falls back to a 1s frame if the event offset lands past the end of a short final segment, so an event clip always gets *a* thumbnail

## 0.7.17
- New **GPX trip viewer** in the Analytics tab. te_usbhub records a GPS track per drive and syncs it as `<trip_id>.gpx` into a `Fahrten/` folder at the share root; when that folder exists, Analytics gains a "Recorded drives (GPX)" section with a trip picker and a Leaflet map that draws the selected track (green start dot, red end dot) with its distance and duration. The section stays hidden entirely when the folder is absent or empty, so it costs nothing on setups without te_usbhub
- New `trips_subpath` option (default `Fahrten`) for the folder name. Per-trip summaries are cached and only re-parsed if a file changes — finished trips never do — so listing is one directory read plus a parse only for trips seen for the first time. Trip ids are validated as bare filenames, so `/api/gpx?id=` can't escape the folder

## 0.7.16
- New **"Fix telemetry sync"** button (Keys panel, section F): re-extracts telemetry for every already-decrypted clip whose cached `telemetry.json` predates the 0.7.15 frame-timing fix, reading only the mp4 already on disk and overwriting just its telemetry sidecar. No re-decryption, no Tesla contact, nothing deleted. A `"schema"` marker written as the first key of each telemetry file lets the preview find stale files with a 64-byte partial read instead of parsing the whole JSON, and a persistent `.telsync_cache.json` means a file confirmed current is never re-read. Shares the progress bar, cancel button and single job slot with the telemetry backfill
- **Fix: the metadata cache was wiped wholesale on every scheduler tick.** `ensure_all()` runs every `interval_seconds` even when there is nothing to decrypt, and its `finally` block unconditionally cleared the whole `_meta_cache`/`_track_cache`. On a large library this meant the scheduler kept nuking the metadata cache every few minutes — often before a slow cold scan had finished rebuilding it — so it could never stay warm and every scan re-read every clip's telemetry/event JSON off the NAS (observed as 100% cache-miss). Now only the clips actually decrypted this round are invalidated; a cycle with nothing to do leaves the cache untouched. The telemetry backfill job had the same wholesale-clear and got the same fix
- Analytics and trips are now **pre-built in the background right after the start-up index**, so the Map and Analytics tabs are ready on first open instead of kicking off a minutes-long build on click. The persistent size/track caches they fill make every later rebuild cheap. Routine stale-refreshes deliberately don't re-trigger this, so the scheduler doesn't re-stat every file each cycle

## 0.7.15
- **Fix: telemetry could lag the video by up to 18 seconds, so the HUD kept showing the car moving well after it had visibly stopped on screen.** Tesla does not embed a telemetry SEI in every video frame — once the car stops, SEIs can simply stop while the video keeps recording for a while longer. The extractor derived fps as (SEI count)/(video duration) and used each SEI's own ordinal for its timestamp, which implicitly assumes one SEI per frame with no gaps. On a real clip only 1511 of 2154 frames carried an SEI (coverage stopped dead at 70% through the clip), which understated fps as ~25.3 instead of the true ~36 and stretched those samples across the full nominal duration — the HUD ended up showing telemetry recorded 9+ seconds earlier by the middle of the clip and 18s earlier by the point telemetry actually stopped
- Every SEI is now tagged with its true position among the video's own H.264 frames (not its position among *other SEIs*), and fps is computed from the true total frame count. Verified against the exact reported case (2026-07-29_18-37-29, front camera): at video time 31s the HUD now reads ~0 km/h, matching the visibly stopped car, where it previously read ~10 km/h carried over from true time 21.7s
- Once real telemetry ends, the HUD now freezes on the last known reading instead of continuing to interpolate stretched values across the rest of the clip
- **Only affects newly extracted telemetry.** Clips already decrypted have their (incorrectly timed) `telemetry.json` cached and are not corrected retroactively — regenerating requires re-decrypting, which "Decrypt everything" only does for clips it hasn't decrypted yet. A bulk "re-extract telemetry for already-decrypted clips" tool was not built pending confirmation it's wanted

## 0.7.14
- New **"Move undecryptable clips aside"** button: files that are encrypted but carry no wrapped key are moved to a separate folder on the NAS, out of the clip list. They are **moved, not deleted** — the folder structure is preserved, so the decision can be undone by moving the folder back
- New `broken_subpath` option (default `broken`), a folder next to the clip tree. It is rejected if it points inside the scanned tree, since the files would simply be indexed again from their new location
- The move is a rename on the same SMB mount: instant, and with no chance of a half-copied file. Only the affected `.mp4` files are moved — `event.json`, thumbnails and the still-recoverable clips in the same folder stay where they are
- Shares the progress bar and cancel button with the other bulk jobs, and only one of them can run at a time

## 0.7.13
- Follow-up to 0.7.12, both found by watching the fix run on a real library: a fetch that has nothing to request now records that outcome, instead of leaving the panel on "Fetching keys…" — exactly the case a library full of key-less files hits every time
- Marking files as key-less now refreshes the clip list, so the red badges and the `no_wrapped_key` counter appear immediately rather than after the next scan

## 0.7.12
- **"Fetching keys…" could sit there forever.** The message was only replaced when at least one key came back, so a fetch that returned none left the panel looking stuck although it had finished. Every outcome is now reported, including "0 new keys", together with how many are still missing
- **Diagnosed why keys never arrive for some files: they contain no key to ask about.** A clip can be eCryptfs-encrypted and still have its wrapped-key section all zeros — and that section *is* the request sent to Tesla. Those files were silently dropped while building the request, so each fetch honestly reported success and the count never moved. On this library that is 273 files, confirmed by sampling headers across eight different dates
- Such files now get their own state instead of being lumped in with "no key yet": a red lock badge explaining that the file contains no key and cannot be decrypted, and a separate `no_wrapped_key` counter alongside `need_keys`
- They are remembered in a new `.nokey_cache.json`, so they are never re-read. Previously every fetch cycle read 8 KB from each of them off the NAS to reach the same conclusion

## 0.7.11
- **Fix: the rectangle area selection on the map could not be drawn.** Marker icons and trip polylines are separate DOM elements sitting above the map and they swallow `mousedown`, so a drag that began on one never reached the map at all — and since 0.7.10 frames the view tightly on the events, almost every drag started on a marker. Markers and paths are now made click-through for the duration of the drag and the cursor turns into a crosshair
- **Shift+drag** now selects an area without arming the button first, and the toolbar says so. The button was the only way in and nothing on the map suggested it had to be pressed
- Releasing the mouse outside the map no longer leaves a half-drawn rectangle with the map stuck in selection mode
- A click without dragging no longer leaves a stray rectangle behind; it simply does nothing, and the tool stays armed for another attempt

## 0.7.10
- New **"Delete originals of already-decrypted clips"** button. `delete_originals` only ever applied at the moment a clip was decrypted, so every clip decrypted before 0.7.8 (when the option did nothing at all) still had its encrypted original on the NAS — on this library 157 GB of them. The button reports how many files and roughly how much space before asking, shows a progress bar, and can be cancelled. It only appears when `delete_originals` is on
- Safety: an original is deleted only when its decrypted copy is present and non-empty, checked immediately before each removal. Candidates come from the index, so building the list costs no NAS access
- **The map now frames your events instead of the whole planet.** It opened at `setView([0,0], 2)` — a world view that also made the rectangle selection almost unusable. It now fits the bounds of the event markers on first open, and "↻ Reset" returns to that overview
- One marker per event instead of one per one-minute segment: an event folder holds up to eleven clips at nearly the same spot, which stacked eleven markers. The triggering segment represents the event
- Selecting the newest trip on first open no longer moves the map — the trip card still fills in, but the view stays on the event overview until you navigate trips yourself

## 0.7.9
- **The segment in which an event actually happened is now marked.** Tesla stores one `event.json` per folder but the footage as one-minute segments, so all four (or eleven) rows of an event carried the same 📅 with nothing to say where the door handle was pulled. The triggering segment gets a 🎯 badge, a highlighted row and a tooltip with the offset ("The event happens in this clip at 0:13"); the surrounding segments keep 📅 with "the trigger is in another segment"
- The trigger is matched to the last segment starting at or before the event, so it stays correct whether segments are 60 or 61 seconds apart and regardless of how short the final one is
- `event.json` is read once per folder per scan instead of once per segment — on this library that is 576 reads instead of 2,370
- This adds `event_ts` to the metadata cache, so the first scan after updating recomputes the metadata of clips that have an event. It runs in the background with the progress bar; the clip list stays usable throughout

## 0.7.8
- **Fix: `delete_originals` never did anything.** The option was read from the config, passed to `run.sh` and printed in the log, but `server.py` only ever assigned `DELETE` and never used it — the sole code path honouring it lives in `pipeline.decrypt_pending()`, which is called exclusively from the unused legacy CLI `main.py`. Encrypted originals were therefore kept forever no matter what the option said. It now works, with guards: an original is removed only after the decrypt returned without raising *and* the decrypted output exists and is non-empty
- **New "Decrypt everything" button** in the Keys panel. `POST /api/decrypt` existed but no UI element ever called it, so the only way to decrypt was one clip at a time from the player. The button shows a progress bar, reports failures, and when `delete_originals` is on it names the consequence and asks for confirmation first — the originals cannot be recovered afterwards
- Fetching missing keys no longer scans the whole tree: candidates come from the index, where a camera marked `locked` already means encrypted-and-keyless. The old path globbed everything and read an 8 KB header from every media file absent from the key store — which is every *plain* clip, since those are never in it. On a library with 8,596 plain files that was ~8,900 SMB reads to find 273 keys, repeated on every scheduler cycle and every button press
- **Prerequisite for the above:** the clip list was built from the source tree alone, so deleting an encrypted original would have made the clip disappear from the viewer entirely — even though the playable decrypted copy was still on the NAS. The list is now the union of both trees; a clip whose source is gone is served straight from `decrypted/` and needs no header read at all
- The decrypt run can be **cancelled** (`POST /api/decrypt/cancel`): files already in flight finish, so a clip is never left half-written, and nothing further is started. The result line reports how much was done before stopping
- **Fix: events were counted per one-minute segment instead of per event.** Tesla saves the rolling buffer as one-minute segments in a single folder sharing one `event.json`, so every event counted once per minute it recorded. On a real library that turned 525 events into 2,154 — object detection alone read 1,558 instead of 437. Both the Analytics chart and the trip card's event counts now count distinct event folders
- Analytics no longer mixes denominators for trips: `total` counts every cluster of clips (mostly parked Sentry sessions), while the distance figures only cover the ones that moved. The tile now reads "410 (50 driven)" and the average is labelled "Avg driven trip", instead of inviting the reading 410 × 6 km

## 0.7.7
- New **filter by event reason**. Two ways in: a dropdown in the Clips filter bar listing the reasons actually present with their counts, or clicking a row in the Analytics *Events by reason* chart — which filters the list and jumps straight to it, like the trip card's "View clips"
- The active reason shows as a removable chip next to the area and trip chips, and combines with search, the Driving/Event/Honk checkboxes, the map area filter and the trip filter, all reflected in the live result count
- Clicking a reason in Analytics clears an active trip filter first: intersecting the two would usually produce an empty list for no visible reason

## 0.7.6
- Fix: the Analytics trip statistics read `0 trips` even when the Map showed plenty. `compute_analytics()` used the non-blocking `trips_cached()`, which answers empty while its own build is running, and that empty answer was then frozen into the analytics cache for a full TTL. Analytics already runs on a background thread, so it now computes the trips itself when the cache is not current
- Fix: Tesla appends the measured magnitude to some trigger reasons (`sentry_aware_accel_0.469145`). Because the number was part of the string, every measurement counted as its own category — a real library showed **14 near-identical rows** of 2–4 events each in the events-by-reason chart instead of one bucket. The magnitude is now split off and reported separately as `reason_value`
- The same suffix meant the UI's label lookup never matched: `REASON_LABELS` is keyed on the bare `sentry_aware_accel`, so the raw string was shown everywhere. Labels now resolve, with the measured value appended, and the labels for door-handle, emergency-braking and the two other manual-save reasons were filled in

## 0.7.5
- Fix: `/api/trips` and `/api/analytics` blocked the request while they were built. On a 3,431-clip library the first `/api/trips` ran for **over 15 minutes** — it reads the telemetry JSON of every clip with GPS data — and `/api/analytics` for **122 seconds**, since it stats all 13,641 camera files. Long enough that the Map and Analytics tabs simply timed out. Both now build in the background with the same stale-while-revalidate contract the clip list already had, and `/api/status` reports `building.trips` / `building.analytics` so the UI reloads them the moment they are ready
- Fix: both caches could be built from the *empty* clip list of a cold start and then serve that for a whole TTL — which is why Analytics showed all zeros right after a first index. Nothing derived is computed until the index exists, and finishing a scan now expires them. `invalidate()` expires analytics too; it previously only expired trips
- A failed build no longer leaves the builder marked as running, so a transient NAS error cannot wedge trips or analytics until the add-on is restarted
- Analytics answers with `pending: true` while it is being built, and the tab explains what is happening instead of showing zeros

## 0.7.4
- The first index now reads **one file per clip instead of six**. A clip's six camera files are written by the car in a single pass and always share an encryption state, so probing one and applying the answer to the rest cuts NAS round trips by 6x — which is what actually decides how long indexing takes
- Reverts the parallel probing added in 0.7.3 to a single reader. Measured on a real SMB share it made things *worse*, not better: 8 parallel readers managed 5.0 files/s where a single one managed 8.3 — the mount serialises the requests and the extra concurrency only adds contention. (The 0.7.3 benchmark that suggested an 8x speedup used a simulated delay, which parallelises perfectly and real CIFS round trips do not.) `ENC_WORKERS` remains available for shares that do benefit, defaulting to 1
- A file that cannot be read is no longer cached as "not encrypted". The probe falls through to the clip's other cameras, and if none can be read the clip is simply retried on the next scan

## 0.7.3
- The eCryptfs classification of new files now runs across 8 threads instead of one. It is pure network latency — 28 bytes per file — so concurrency, not bandwidth, decides how long a first index takes. Measured under simulated NAS latency: 8x faster, near-linear; on a 13,625-file share this turns ~28 minutes into a few
- Fix (introduced in 0.7.2): static assets were served with `Cache-Control: max-age=86400`, so after an add-on update browsers kept running the *previous* `app.js` for up to a day. They now revalidate via `ETag`/`304`, and the asset URLs carry a `?v=` matching the add-on version so already-cached copies are bypassed immediately
- The map is no longer built during page load — it is created the first time the Map tab is opened. Loading the viewer now issues **no external requests at all**; map tiles are fetched only once you look at the map
- `api/login/url` is no longer awaited during start-up; it only fills the login link in the Keys panel and was delaying the status line and the clip list
- Removed `/api/all_gps`, dead since 0.6.0 — the map reads clip positions straight from `/api/clips`
- README: the add-on options are now documented in full, grouped by what they affect (NAS connection, decryption, behaviour), each with its default, plus a note on what the first start actually does

## 0.7.2
- Fix (regression in 0.7.1): the scheduler and the batch jobs called `_scan()` directly, bypassing the single-flight guard, so with `auto_decrypt` on a **second** full scan started every `interval_seconds` on top of the one already running. Several scans then competed for the same SMB mount and overwrote each other's progress — measured on a 13,625-file share, the index build dropped from ~7 files/s to ~1.25 files/s. They now use the cached list, and `_scan()` additionally serialises itself
- The eCryptfs classification is checkpointed to `.enc_cache.json` every 500 files. Previously the cache was only written when a scan finished, so restarting the add-on during a long first index threw away all of the work
- Leaflet 1.9.4 (JS, CSS and its images) now ships inside the add-on image instead of being pulled from `unpkg.com`. It was a render-blocking `<script>` in `<head>`, so an HA host without internet — or a slow CDN — stalled the whole viewer before any of its own code ran. It is now also `defer`red, and still executes before `app.js`
- Static files are served with a `Cache-Control` header, correct content types for images, and support sub-paths (`static/images/…`) with an explicit path-traversal check

## 0.7.1
- Fix: loading clips could take minutes (or appear to hang entirely) on installs with a lot of footage on a network share. `/api/status` and `/api/clips` re-scanned the whole TeslaCam tree from scratch every 15 seconds, and each scan opened *every* MP4 over SMB to read its 28-byte eCryptfs header — thousands of network round trips per request, with all other requests queued behind the scan lock
- The scan now walks each directory once with `os.scandir` (one SMB round trip per folder) instead of issuing an `os.path.exists()` per camera file
- New persistent caches in `/data`, all keyed by write-once paths so they stay valid across restarts: `.enc_cache.json` (eCryptfs header per file — a file is now read at most once, ever), `.track_cache.json` (GPS tracks, so `/api/trips` no longer re-reads every telemetry JSON off the NAS on each call), `.size_cache.json` (clip sizes for the Analytics storage stats). Stale entries are pruned when files disappear
- Requests are never blocked by a scan any more: a stale clip list is served immediately while it refreshes in the background, and the list is warmed at start-up rather than on the first page load
- `/api/trips` is now cached like `/api/clips` and `/api/analytics` instead of being rebuilt on every call
- Clip-list cache lifetime raised from 15 s to 120 s, analytics from 60 s to 300 s — with background refresh, a short TTL only caused redundant scans
- New progress bar under the header while the clip index is being built, with the current phase (indexing folders → reading clip states → reading telemetry & events), a count and a percentage. The folder walk shows as indeterminate because it cannot know its total until it has finished. The clip list reloads by itself the moment the scan completes
- The batch thumbnail and telemetry jobs now show the same progress bar instead of a bare `12/340…` counter
- `/api/status` reports the new `scan_job` and `ready` fields; `ready` lets the UI say "still indexing" instead of showing an empty list as if the share had no clips

## 0.7.0
- The clip list is the default view again (new **Clips** tab), with **Map** and **Analytics** as secondary tabs — the map is no longer the landing page
- Map area selection: draw a rectangle, see how many clips fall inside, and jump straight to the filtered list ("View list"). The active area filter shows as a removable chip in the Clips view
- All filters combine: an area filter from the map plus the Driving / Event / Honk checkboxes narrow the list together, with a live result count
- Trip card's "View clips" now filters the Clips list to that trip (removable chip) instead of a separate panel
- Removed the slide-out browser panel (its Events/Trips/All-Clips sub-tabs are covered by the Clips list filters and the map's trip card)

## 0.6.2
- New `debug_logging` option: verbose add-on log with per-request timing, `_scan`/`build_trips`/`compute_analytics` duration, and metadata-cache hit/miss counts (off by default; the previous silent `log_message` made incidents like 0.6.0's hang invisible in the log)
- The web UI now flags "still loading… taking longer than expected" instead of sitting silently if the initial load takes more than 8 seconds

## 0.6.1
- Fix: the 0.6.0 metadata-cache upgrade (for GPS "track" data) forced a synchronous full re-scan of every historical clip's telemetry/event JSON on the first request after updating, which could hang the UI ("loading" forever) on installs with a lot of accumulated footage on a network share. GPS tracks for trip routes are now read on demand inside `/api/trips` instead of being persisted in the shared clip cache, so `/api/status`/`/api/clips` are unaffected and existing caches stay valid across the update.

## 0.6.0
- New map-centric landing page with trip routes, event markers, and a slide-out clip browser panel (replaces the small GPS filter panel; rectangle-select-to-filter preserved on the new map)
- Trips: clips are grouped into drives by a 20-minute gap-threshold algorithm, with distance (GPS haversine, no odometer data available) and event counts; floating trip card with prev/next navigation
- New Analytics tab: storage usage per folder/vehicle, trip/clip statistics, events-by-reason and clips-by-month charts (dependency-free inline SVG/CSS, no new CDN dependency)
- Light/dark theme toggle (persisted, respects OS preference on first load) alongside the existing dark theme
- New inline SVG icon set for map markers, tab navigation, and the theme toggle (existing emoji elsewhere unchanged)
- index.html split into index.html + app.js + style.css, served via the existing /static/ route
- New GET /api/trips and GET /api/analytics endpoints

## 0.4.23
- All UI text and log messages translated to English
- HACS compatible (repository.json, hacs.json)

## 0.4.22
- README rewritten: viewer-first description with optional decryption

## 0.4.21
- Sidebar filters: "Driving" (clips with SEI telemetry) and "Event" (clips with event.json) replace the old "locked" filter

## 0.4.20
- "Nerd info" panel shows event metadata (trigger reason, location, camera) for all clips with event.json — even without SEI telemetry

## 0.4.19
- GPS map shown for clips with event.json location (even without driving telemetry)
- /api/event returns full event data (GPS, reason, city, street, camera)

## 0.4.18
- Autopilot indicator hidden when inactive

## 0.4.17
- Brake indicator only red when active (replaced emoji with CSS-styleable symbol)

## 0.4.16
- index.html served with Cache-Control: no-cache (fixes stale UI after updates)

## 0.4.15
- Heatmap replaced with clickable marker dots per clip
- New green accelerator bar in HUD
- Brake indicator dimmed when inactive

## 0.4.14
- Persistent metadata cache (.meta_cache.json) — much faster startup after first scan
- leaflet-draw replaced with native rectangle selection (fixes HA Ingress CSP block)

## 0.4.13
- Fixed: plain (unencrypted) clips never got SEI telemetry extracted automatically — `/api/prepare` (which runs telemetry extraction) was only triggered when a clip had an encrypted camera. Now triggered transparently in the background when opening a plain clip without cached telemetry.
- New: "🛰️ Extract all telemetry" batch button (Keys panel, step E) to backfill telemetry for all existing plain clips that are missing it.
- New: `POST /api/telemetry_all` endpoint + `tel_job` progress in `/api/status`.

## 0.4.12
- Loading indicator in sidebar ("⏳ Loading clips…") and header while the clip list fetches, so large libraries (1000s of clips) don't look stuck/empty

## 0.4.11
- Fixed: duplicate `id="tools"` element (stray leftover markup) broke the Keys/Decryption panel lookup

## 0.4.7
- GPS heatmap filter in sidebar (Leaflet + leaflet.heat + leaflet-draw)
- GPS coordinates from `event.json` as fallback for clips without telemetry
- Heatmap hidden by default, toggled via 🗺️ button in header
- CartoDB dark tiles (no Referer restriction, works inside HA ingress)

## 0.4.6
- Batch thumbnail generation for all clips with telemetry or event data
- Player auto-seeks to event timestamp from `event.json`
- `/api/event?id=` endpoint returns seek offset in seconds
- Fixed: event seek position lost on Play click (initialSeek variable)
- Fixed: ffmpeg `-f image2` flag for `.part` temp file format detection

## 0.4.5
- PKCE OAuth with refresh token for automated key re-fetch
- Direct API: batch size fixed to 30 (Tesla API maximum)
- Fixed: login URL populated dynamically via `/api/login/url`
- Fixed: HTML element IDs for login link and textarea

## 0.4.0
- Unified clip viewer: all clips (encrypted, plain, decrypted) in one list
- On-demand decryption via POST `/api/prepare`
- Telemetry HUD (gear, speed, steering, blinkers, brake, autopilot)
- Persistent FEK keystore (`.teslacam_keys.json` on NAS, never deleted)
- Thumbnail generation with ffmpeg at event timestamp

## 0.1.0
- Initial release: browser-bridge architecture
- Local eCryptfs decryption (AES-128-CBC) + SEI telemetry extraction
- FEKs via browser bookmarklet (dashcam.tesla.com), no Tesla login in container
- Ingress viewer with multi-camera layout + telemetry HUD
