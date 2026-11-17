FROM python:3.12-slim
ENV PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY contoso/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY contoso_backend/__init__.py contoso_backend/auth.py contoso_backend/errors.py ./contoso_backend/
COPY contoso/__init__.py contoso/knowledge.py contoso/bounded.py contoso/configuration.py contoso/entrypoint.py contoso/identity_probe.py contoso/identity_link.py contoso/token_client.py contoso/api_center.py ./contoso/
COPY contoso/router/ ./contoso/router/
COPY contoso/providers/ ./contoso/providers/
COPY contoso-demo/scenarios.json ./contoso-demo/scenarios.json
COPY contoso-demo/motors-scenarios.json ./contoso-demo/motors-scenarios.json
USER 10001:10001
EXPOSE 8088
CMD ["python", "-m", "contoso.entrypoint"]
