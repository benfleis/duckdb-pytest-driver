# syntax=docker/dockerfile:1

ARG MINIO_IMAGE=minio/minio:RELEASE.2025-09-07T16-13-09Z
ARG MC_IMAGE=minio/mc:RELEASE.2025-08-13T08-35-41Z

FROM ${MC_IMAGE} AS mc
FROM ${MINIO_IMAGE}

COPY --from=mc /usr/bin/mc /usr/bin/mc
