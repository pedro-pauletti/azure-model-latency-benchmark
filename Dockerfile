FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
RUN useradd --uid 10001 --create-home runner
COPY *.py *.json dashboard.template.html ./
USER 10001
ENTRYPOINT ["python", "-u", "cloud_worker.py"]
