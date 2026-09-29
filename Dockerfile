FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml /build/
COPY src /build/src
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip build \
 && python -m build --wheel --outdir /dist /build

FROM python:3.12-slim
RUN groupadd -g 1030 groundcrew \
 && useradd -u 1030 -g 1030 -m -s /sbin/nologin groundcrew
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/*.whl \
 && rm /tmp/*.whl
# Group 0 ownership with g=u keeps these dirs writable when the platform
# assigns an arbitrary non-root UID instead of honouring USER.
RUN mkdir -p /var/groundcrew/state /etc/groundcrew \
 && chown -R 1030:0 /var/groundcrew /etc/groundcrew \
 && chmod -R g=u /var/groundcrew /etc/groundcrew
USER 1030:1030
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["artifactory-groundcrew"]
