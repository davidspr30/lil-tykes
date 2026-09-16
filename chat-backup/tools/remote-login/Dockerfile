# Temporary helper: the chat-backup image plus a VNC server and noVNC web viewer,
# so the one-time ChatGPT login can be done from a phone browser.
FROM chat-backup-chatbackup

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends x11vnc novnc python3-websockify \
 && rm -rf /var/lib/apt/lists/*

COPY start.sh remote_login.py /helper/
RUN chmod +x /helper/start.sh

USER pwuser
ENTRYPOINT ["/helper/start.sh"]
