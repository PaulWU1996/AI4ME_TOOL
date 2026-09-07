FROM python:3.10-slim

WORKDIR /app

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ .
COPY dag/ ./dag

EXPOSE 9000

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","9000", "--proxy-headers"]