FROM python:3.11-slim
WORKDIR /app
COPY requirements_v2.txt .
RUN pip install --no-cache-dir -r requirements_v2.txt
COPY . .
CMD ["python", "start.py"]
