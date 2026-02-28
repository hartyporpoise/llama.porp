FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY porpulsion/ porpulsion/
COPY templates/ templates/
COPY static/ static/

CMD ["python", "-m", "porpulsion.agent"]
