#!/bin/bash

# Start noVNC with CORS headers
# This allows the VNC to be embedded in iframes from different origins

PORT=${NO_VNC_PORT:-6080}

echo "Starting noVNC with CORS support on port $PORT"

# Create a simple HTTP server with CORS headers
python3 -m http.server $PORT --bind 0.0.0.0 &
HTTP_SERVER_PID=$!

# Start the noVNC proxy
/usr/local/novnc/utils/novnc_proxy \
    --vnc localhost:5900 \
    --listen $PORT \
    --web /usr/local/novnc \
    --heartbeat 30 \
    --record /tmp/novnc.log &

NOVNC_PID=$!

# Function to add CORS headers to noVNC responses
add_cors_headers() {
    # Wait a moment for noVNC to start
    sleep 2
    
    # Use socat to add CORS headers to the noVNC proxy
    while true; do
        socat TCP-LISTEN:$((PORT+1)),reuseaddr,fork \
            TCP:localhost:$PORT,retry=1 \
            < <(echo -e "HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\n\r\n") &
        sleep 1
    done
}

# Start CORS header injection
add_cors_headers &
CORS_PID=$!

# Wait for any process to exit
wait -n

# Clean up
kill $HTTP_SERVER_PID $NOVNC_PID $CORS_PID 2>/dev/null 