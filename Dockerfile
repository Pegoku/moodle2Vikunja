FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY m2v ./m2v
ENTRYPOINT ["python", "-m", "m2v"]
CMD ["--loop", "30"]
