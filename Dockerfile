# Use Python 3.11 slim image
FROM python:3.11-slim

# Instala dependências básicas
RUN apt-get update && apt-get install -y gcc

# Timezone UTC: a lib `schedule` usa datetime.now() local naive; fixar TZ=UTC
# garante que "local" == "UTC" e evita o bug clássico de agendamento fora de UTC.
# python:3.11-slim já usa UTC por default; ENV explícito documenta o contrato.
ENV TZ=UTC

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scheduler_api.py .
COPY .env .

# Expose port 8000
EXPOSE 8000

# Run the application
CMD ["python", "scheduler_api.py"]
