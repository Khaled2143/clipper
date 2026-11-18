FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY clipmatch/ clipmatch/
RUN python -m pip install --no-cache-dir ".[live]"
CMD ["clipmatch-live", "watch"]
