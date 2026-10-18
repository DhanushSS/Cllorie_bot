FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Durable state is supplied by a named volume at /data.
ENV DB_PATH=/data/bot_state.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
CMD ["python", "bot.py"]
