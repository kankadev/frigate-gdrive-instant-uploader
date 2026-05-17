FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends nano sqlite3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Container-level healthcheck. Probes the in-process HTTP healthcheck server
# from inside the container (127.0.0.1) so the port does not need to be
# exposed to the host. We use stdlib urllib instead of curl to avoid adding
# another package to the image. Exits non-zero on any HTTP status != 200,
# any network error, or any timeout — that's the contract Docker expects.
#
# - interval=30s: probe cadence after the container is healthy
# - timeout=10s:  the probe itself must finish within 10s (DB query is cheap)
# - start-period=120s: grace period at boot. Initial scheduler jobs run 90s
#   after start; this gives the app a comfortable margin to become healthy
#   before failures count toward `retries`.
# - retries=3:   3 consecutive failures (~90s) flip the container to unhealthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import sys, urllib.request, urllib.error; \
        url = 'http://127.0.0.1:' + __import__('os').getenv('HEALTHCHECK_PORT', '8080') + '/health'; \
        try: \
            code = urllib.request.urlopen(url, timeout=5).getcode(); \
            sys.exit(0 if code == 200 else 1); \
        except urllib.error.HTTPError as e: \
            sys.exit(0 if e.code == 200 else 1); \
        except Exception: \
            sys.exit(1)"

CMD ["python", "main.py"]
