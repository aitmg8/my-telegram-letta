# Option A (explicit /app paths)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY main.py /app/
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
