# ─────────────────────────────────────────────────────────────────────────────
# Embroidery Business MCP Server — Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# Installs the Microsoft ODBC 18 driver for SQL Server so pyodbc can connect
# to your existing MSSQL instance (or the optional local mirror in compose).
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

# ── System deps + Microsoft ODBC Driver 18 ────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        apt-transport-https \
        unixodbc \
        unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────
COPY . .

# ── Runtime ───────────────────────────────────────────────────────────────
EXPOSE 8000

# MCP_HOST / MCP_PORT / DB_* are supplied via docker-compose env_file
CMD ["python", "server.py"]
