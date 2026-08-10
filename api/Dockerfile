FROM python:3.11-slim

WORKDIR /code

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY api/app ./app
COPY api/alembic ./alembic
COPY api/alembic.ini .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
