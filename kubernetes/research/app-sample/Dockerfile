FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask gunicorn

COPY app.py /app/app.py

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "2", "app:app"]
