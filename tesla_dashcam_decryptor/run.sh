#!/usr/bin/with-contenv bashio
set -e

HOST=$(bashio::config 'smb_host')
SHARE=$(bashio::config 'smb_share')
CLIPS=$(bashio::config 'clips_subpath')
if ! bashio::config.has_value 'clips_subpath'; then CLIPS="TeslaCam"; fi
ENC=""
if bashio::config.has_value 'enc_subpath'; then ENC=$(bashio::config 'enc_subpath'); fi
DEC="decrypted"
if bashio::config.has_value 'dec_subpath'; then DEC=$(bashio::config 'dec_subpath'); fi
BROKEN="broken"
if bashio::config.has_value 'broken_subpath'; then BROKEN=$(bashio::config 'broken_subpath'); fi
TRIPS="Fahrten"
if bashio::config.has_value 'trips_subpath'; then TRIPS=$(bashio::config 'trips_subpath'); fi
INTERVAL=$(bashio::config 'interval_seconds')
DELETE=$(bashio::config 'delete_originals')
AUTO=$(bashio::config 'auto_decrypt')
DIRECT=$(bashio::config 'enable_direct_api')
KEYMODE=$(bashio::config 'key_after_decrypt')
DEBUG=$(bashio::config 'debug_logging')
VERS="3.0"
if bashio::config.has_value 'smb_version'; then VERS=$(bashio::config 'smb_version'); fi

export DATA_DIR=/data
MNT=/mnt/nas
mkdir -p "${MNT}"

OPTS="rw,iocharset=utf8,vers=${VERS},uid=0,gid=0,file_mode=0660,dir_mode=0770,nofail"
if bashio::config.has_value 'smb_username'; then
  # pass credentials directly (no file -> no DAC read problem in container)
  OPTS="${OPTS},username=$(bashio::config 'smb_username'),password=$(bashio::config 'smb_password')"
  if bashio::config.has_value 'smb_domain'; then
    OPTS="${OPTS},domain=$(bashio::config 'smb_domain')"
  fi
else
  OPTS="${OPTS},guest"
fi

bashio::log.info "Te_FITI - Fleet Integration, Telemetry & Infotainment (Hybrid: Direct API + Bookmarklet)"
bashio::log.info "Mounting //${HOST}/${SHARE} (SMB ${VERS}) -> ${MNT}"
if ! mount -t cifs "//${HOST}/${SHARE}" "${MNT}" -o "${OPTS}"; then
  bashio::log.warning "Mount failed - CIFS kernel message (dmesg):"
  dmesg 2>/dev/null | grep -iE 'cifs|smb' | tail -12 || true
  bashio::log.fatal "CIFS mount failed. Check host/share/credentials/SMB version."
  exit 1
fi

SCAN="${MNT}/${CLIPS}"
if [ -n "${ENC}" ]; then
  SRC="${MNT}/${ENC}"
else
  SRC="${SCAN}"
fi
OUT="${MNT}/${DEC}"
mkdir -p "${OUT}"
# Sits next to the clip tree, not inside it, so moved files are not re-indexed.
BROKENDIR="${MNT}/${BROKEN}"
# GPX trips from te_usbhub live at the share root too. Only pass the path when
# the folder actually exists, so the viewer stays hidden on shares without it.
TRIPSDIR="${MNT}/${TRIPS}"
bashio::log.info "Scan  : ${SCAN}"
bashio::log.info "Source: ${SRC} (encrypted files auto-detected by header)"
bashio::log.info "Target: ${OUT}   Keys: ${SRC}/.teslacam_keys.json"
bashio::log.info "auto_decrypt=${AUTO} direct_api=${DIRECT} key_after_decrypt=${KEYMODE} delete_originals=${DELETE}"

FLAGS=""
[ "${DELETE}" = "true" ] && FLAGS="${FLAGS} --delete"
[ "${AUTO}" != "true" ] && FLAGS="${FLAGS} --no-auto-decrypt"
[ "${DIRECT}" != "true" ] && FLAGS="${FLAGS} --no-direct-api"
[ "${KEYMODE}" = "embed" ] && FLAGS="${FLAGS} --embed-key"
[ "${DEBUG}" = "true" ] && FLAGS="${FLAGS} --debug"

trap 'umount "${MNT}" 2>/dev/null || true' EXIT INT TERM

exec python3 /app/server.py --src "${SRC}" --out "${OUT}" --scan "${SCAN}" --broken "${BROKENDIR}" --trips "${TRIPSDIR}" --port 8099 --interval "${INTERVAL}" ${FLAGS}
