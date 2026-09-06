FROM python:3.12-slim

WORKDIR /app
COPY fake_fmg_server.py ./
COPY certs/ ./certs/

EXPOSE 8443
CMD ["python", "fake_fmg_server.py"]
