FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /srv

COPY app/ /srv/app/
COPY frameassets/ /srv/frameassets/

RUN pip install --no-cache-dir -r /srv/app/requirements.txt

RUN python -m playwright install --with-deps chromium

ENV PYTHONPATH=/srv

CMD bash -lc "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"
