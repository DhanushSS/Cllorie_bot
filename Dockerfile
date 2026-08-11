FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite file for session state lives in the container's writable layer.
# Meal logs themselves live in Google Sheets, so a redeploy losing this
# file just re-opens today's earliest unlogged meal - harmless.
CMD ["python", "bot.py"]
