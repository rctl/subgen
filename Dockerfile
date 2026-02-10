FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY subgen ./subgen
COPY README.md ./

ENV PYTHONUNBUFFERED=1
ENV SUBGEN_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "subgen.web", "--media-dir", "/media", "--endpoint", "https://stt.rtek.dev", "--host", "0.0.0.0", "--port", "8080"]
