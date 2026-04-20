FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt bot.py ./
RUN mkdir -p assets data \
    && pip install --no-cache-dir -r requirements.txt

# Banner is optional. Build never requires header.png.
# Option A — add to repo an assets/ folder with header.png, then uncomment:
# COPY assets ./assets
# Option B — Railway volume on /app/assets with header.png, or env BOT_HEADER_IMAGE

CMD ["python", "bot.py"]
