FROM python:3.12-slim

RUN groupadd -g 1001 sikim && useradd -u 1001 -g 1001 -m sikim

RUN apt-get update && apt-get install -y --no-install-recommends \
        sudo \
        systemd \
        util-linux \
    && echo "sikim ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/sikim \
    && chmod 440 /etc/sudoers.d/sikim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

USER sikim

CMD ["python", "bot.py"]
