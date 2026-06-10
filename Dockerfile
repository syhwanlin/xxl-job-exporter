# XXL-JOB Prometheus Exporter
FROM python:3.12-slim

LABEL org.opencontainers.image.title="xxl-job-exporter" \
      org.opencontainers.image.description="Non-invasive Prometheus exporter for XXL-JOB (reads the xxl-job DB with a read-only account)"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

# Run as non-root
RUN useradd --system --uid 10001 --no-create-home exporter
USER 10001

EXPOSE 9588

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9588/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["python", "exporter.py"]
