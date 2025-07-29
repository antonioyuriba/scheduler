# Use Python 3.11 slim image
FROM python:3.11-slim

# Instala dependências básicas
RUN apt-get update && apt-get install -y gcc

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
