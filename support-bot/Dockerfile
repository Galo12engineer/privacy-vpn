# privacyvpn support relay bot — zero-deps (stdlib only), non-root, long-polling.
FROM python:3.12-slim

# non-root + a writable state directory (a named volume inherits ownership from the image)
RUN useradd -u 10002 -m bot && mkdir -p /data && chown bot:bot /data
USER bot
WORKDIR /app
COPY support-bot/bot.py .

ENV SUPPORT_BOT_STATE_FILE=/data/relay-state.json
VOLUME /data
# long polling → no inbound ports; outbound HTTPS to api.telegram.org
CMD ["python", "-u", "bot.py"]
