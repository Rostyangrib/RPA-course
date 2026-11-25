# Собирает образ с зависимостями, кодом робота и файлами веб-панели.
FROM python:3.13-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py config.toml ./rpa_course/
COPY certs ./rpa_course/certs
COPY data/price.xlsx ./rpa_course/data/price.xlsx
COPY templates ./rpa_course/templates
COPY web_assets ./rpa_course/web_assets
RUN mkdir -p /app/rpa_course/.runtime && chown 10001:10001 /app/rpa_course/.runtime

USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "rpa_course.web", "--host", "0.0.0.0"]
