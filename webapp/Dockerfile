# Evidence viewer as a container (e.g. a Render web service):
#
#   docker build -t evidence-viewer webapp
#   docker run -p 8000:8000 -e DATABASE_URL_CLOUD=postgresql://... evidence-viewer
#
# Reads from Supabase (WEBAPP_DB=cloud); the connection string comes from the
# DATABASE_URL_CLOUD environment variable, never the image.
FROM python:3.12-slim

# Run as an unprivileged user.
RUN useradd -m -u 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    WEBAPP_DB=cloud
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user app ./app
COPY --chown=user static ./static

# Render (and most hosts) pass the port in $PORT.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
