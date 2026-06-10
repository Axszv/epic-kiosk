FROM python:3.10-slim

WORKDIR /app

RUN mkdir -p /app/data

RUN pip install --no-cache-dir \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    fastapi uvicorn redis apscheduler aiofiles python-multipart jinja2 httpx

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
