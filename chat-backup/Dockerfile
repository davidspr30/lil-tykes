# The Playwright image ships Python, Chromium, Xvfb and the "pwuser" account.
# Keep this tag and requirements.txt on the same Playwright version.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chatbackup/ chatbackup/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Run as a normal user so Chromium keeps its sandbox. The data folder on the
# host must be owned by uid 1000 (see README).
USER pwuser

ENTRYPOINT ["/app/entrypoint.sh"]
