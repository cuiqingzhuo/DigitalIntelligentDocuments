FROM python:3.12-slim
WORKDIR /srv/diw
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DIW_RUNTIME=/srv/diw/runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-train.txt ./
ARG DIW_WITH_OBB=0
RUN pip install --no-cache-dir -r requirements.txt && if [ "$DIW_WITH_OBB" = "1" ]; then pip install --no-cache-dir "ultralytics>=8.4,<9"; fi
RUN useradd --system --create-home diw && install -d -o diw -g diw /srv/diw/runtime
COPY app ./app
COPY web ./web
COPY models ./models
USER diw
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
