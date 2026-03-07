FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grist_coder.py .

# Port du service
EXPOSE 8742

CMD ["uvicorn", "grist_coder:app", "--host", "0.0.0.0", "--port", "8742"]
