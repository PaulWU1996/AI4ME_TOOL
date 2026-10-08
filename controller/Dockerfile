FROM python:3.10-slim

WORKDIR /app

# Celery's app loader puts the working directory on sys.path only while it
# imports the app module, then removes it again -- so a lazy `import dag`
# inside a task body (worker/tasks.py:461) fails at run time even though the
# package sits right there in /app. Setting PYTHONPATH makes /app importable
# for the whole process lifetime, not just during app discovery.
ENV PYTHONPATH=/app

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ .
COPY dag/ ./dag

EXPOSE 9000

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","9000", "--proxy-headers"]