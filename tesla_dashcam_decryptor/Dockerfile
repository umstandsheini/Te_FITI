ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python + AES (py3-cryptography as pre-built Alpine package -> no compilation on ARM)
# cifs-utils for the SMB/NAS mount
RUN apk add --no-cache python3 py3-cryptography cifs-utils ffmpeg

WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# keys.json / cache live in the persistent /data directory of the add-on
ENV DATA_DIR=/data \
    PYTHONPATH=/app

CMD [ "/run.sh" ]
