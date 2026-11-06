FROM python:3.12-slim

WORKDIR /app
COPY --chown=10001:10001 index.html server.py ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]
