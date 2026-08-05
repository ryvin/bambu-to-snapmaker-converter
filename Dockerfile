# Bambu to Snapmaker U1 Converter
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir flask

# Copy application files
COPY app.py .
COPY history.py .
COPY split_plates.py .
COPY refix.py .
COPY templates/ templates/
COPY u1_template.3mf .
COPY u1_template_supports.3mf .
COPY filament_types.3mf .

# Create directories for uploads and converted files
RUN mkdir -p uploads converted_u1

# Expose port
EXPOSE 8080

# Run the application
CMD ["python", "app.py"]
