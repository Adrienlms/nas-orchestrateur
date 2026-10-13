FROM python:3.13-slim

# Node.js + Claude Code : permet d'utiliser le quota de ton abonnement Claude.
# Construis avec --build-arg INSTALL_CLAUDE_CODE=false pour une image plus légère.
ARG INSTALL_CLAUDE_CODE=true
RUN if [ "$INSTALL_CLAUDE_CODE" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && npm install -g @anthropic-ai/claude-code \
      && npm cache clean --force \
      && apt-get purge -y npm && apt-get autoremove -y \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY super_agent ./super_agent
COPY config.toml ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# /data contient la configuration, l'historique des tâches et la connexion Claude Code.
ENV SUPER_AGENT_DATA=/data \
    HOME=/data/home \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "super_agent", "web", "--port", "8080"]
