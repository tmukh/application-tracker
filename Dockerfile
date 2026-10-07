# Application Tracker image. Build:  docker build -t application-tracker .
FROM python:3.12-slim

# Python libraries only, no compiler needed. Dependencies come first so code changes reuse this cached layer.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Run as an ordinary user: if the dashboard were ever broken into, it would not be root in the container.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tracker \
 && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY tracker/ tracker/

# 0.0.0.0 is correct INSIDE a container (the container has its own network). Who can reach the container is
# decided outside: Kubernetes Service / NetworkPolicy / Tailscale. Everything that changes lives in /data,
# so that is the only folder that needs a volume.
ENV TRACKER_HOST=0.0.0.0 \
    TRACKER_PORT=5055 \
    TRACKER_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 5055
USER 10001
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5055/healthz', timeout=2).status==200 else 1)"
CMD ["python", "-m", "tracker"]
