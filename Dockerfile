FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=7878
EXPOSE 7878

CMD ["sh", "-c", "uvicorn api.index:app --host 0.0.0.0 --port ${PORT}"]
