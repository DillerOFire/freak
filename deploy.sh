#!/bin/bash

# Build the image (uses uv internally)
docker build -t telegram-group-bot .

# Run the container
# Mount the database file to persist memory
docker run -d \
    --name telegram-group-bot \
    --restart unless-stopped \
    --env-file .env \
    -v $(pwd)/bot_memory.db:/data/bot_memory.db \
    telegram-group-bot

echo "Bot deployed successfully!"
