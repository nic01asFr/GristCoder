FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grist_coder.py .
COPY widget.html .
COPY harness/ ./harness/

# Port du service (surchargable via $PORT, injecte par le chart Onyxia)
EXPOSE 8742

CMD ["sh", "-c", "uvicorn grist_coder:app --host 0.0.0.0 --port ${PORT:-8742}"]
