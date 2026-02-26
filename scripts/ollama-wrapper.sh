#!/bin/sh
# ollama-wrapper.sh — Entrypoint for the Ollama sidecar.
#
# Sources /shared/ollama.env (written by porpulsion) before starting Ollama,
# then watches for /shared/restart to re-exec with updated env.
#
# The /shared directory must be a volume shared with the porpulsion container.

ENV_FILE="/shared/ollama.env"
SENTINEL="/shared/restart"

start_ollama() {
  # Source the env file if present.  `set -a` auto-exports every variable that
  # gets set so that `ollama serve` (a child process) inherits them all.
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
  # Remove any stale sentinel before starting so we don't immediately loop.
  rm -f "$SENTINEL"
  # Start Ollama in the background.
  ollama serve &
  OLLAMA_PID=$!
  echo "ollama-wrapper: started ollama (pid $OLLAMA_PID)"
}

start_ollama

# Watch for the restart sentinel. When porpulsion writes it, stop Ollama and
# re-exec with the new env from the updated env file.
while true; do
  sleep 2
  if [ -f "$SENTINEL" ]; then
    echo "ollama-wrapper: restart sentinel detected, restarting ollama..."
    kill "$OLLAMA_PID" 2>/dev/null
    wait "$OLLAMA_PID" 2>/dev/null
    start_ollama
  fi
  # If Ollama died unexpectedly, restart it.
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    echo "ollama-wrapper: ollama exited unexpectedly, restarting..."
    start_ollama
  fi
done
