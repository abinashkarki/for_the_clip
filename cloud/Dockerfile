FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY clippie_cloud ./clippie_cloud
RUN useradd --system --uid 10001 clippie && mkdir /data && chown clippie /data
USER clippie
ENV CLOUD_DB=/data/cloud.sqlite3
VOLUME /data
EXPOSE 8080
# TLS terminates at the platform's proxy; set CLOUD_REQUIRE_HTTPS=1 to reject plain-HTTP requests.
CMD ["uvicorn", "--factory", "clippie_cloud.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
