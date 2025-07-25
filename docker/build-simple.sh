#!/bin/bash

echo "Building magentic-ui-python-env image..."
cd magentic-ui-python-env
docker build -t magentic-ui-python-env:latest .

echo "Building magentic-ui-browser-docker image..."
cd ../magentic-ui-browser-docker
docker build -t magentic-ui-browser-docker:latest .

echo "Both builds completed successfully!" 