FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY fetcher.py requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# accounts.yml is mounted at runtime via docker-compose volume, not baked in.
CMD ["python", "fetcher.py"]
