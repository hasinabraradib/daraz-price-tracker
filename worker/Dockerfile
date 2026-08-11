FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /code

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY worker/worker_app ./app

CMD ["python", "-m", "app.main"]
