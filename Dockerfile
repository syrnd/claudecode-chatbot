FROM python:3.12-slim

RUN groupadd -g 1001 sikim && useradd -u 1001 -g 1001 -m sikim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

USER sikim

CMD ["python", "bot.py"]
