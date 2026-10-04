FROM python:3.12-slim

WORKDIR /app

# Le serveur Prefect éphémère peut mettre plus de 20 s à démarrer dans un conteneur
ENV PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS=120

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY etl_prefect.py .
COPY data/data.csv data/data.csv

CMD ["python", "etl_prefect.py"]
