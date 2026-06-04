FROM python:3.14-slim

WORKDIR /app

# Install system dependencies required by lxml and other native packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY api.py .
COPY scrapers ./scrapers
COPY resolvers ./resolvers
COPY metadata_engine ./metadata_engine

# Expose FastAPI default port
EXPOSE 8000

# Run the application with uvicorn
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]