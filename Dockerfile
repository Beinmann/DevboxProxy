FROM python:3.12-slim
RUN pip install --no-cache-dir aiohttp==3.*
COPY proxy/app.py /app/app.py
COPY connector/connector.py /app/connector.py
USER nobody
CMD ["python", "/app/app.py"]
