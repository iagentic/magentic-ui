#!/usr/bin/env python3
"""
CORS proxy for noVNC to allow cross-origin requests from Magentic-UI
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import urllib.error
import sys
import os

class CORSProxy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Forward the request to the actual noVNC server
        target_url = f"http://localhost:6080{self.path}"
        
        try:
            # Create request
            req = urllib.request.Request(target_url)
            
            # Copy headers
            for header, value in self.headers.items():
                if header.lower() not in ['host', 'connection']:
                    req.add_header(header, value)
            
            # Make the request
            with urllib.request.urlopen(req) as response:
                # Read the response
                content = response.read()
                
                # Send CORS headers
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.send_header('Content-Type', response.headers.get('Content-Type', 'text/html'))
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                
                # Send the content
                self.wfile.write(content)
                
        except Exception as e:
            print(f"Error proxying request: {e}")
            self.send_error(500, f"Proxy error: {e}")
    
    def do_OPTIONS(self):
        # Handle preflight CORS requests
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_POST(self):
        # Handle POST requests (for WebSocket upgrades)
        self.do_GET()

def main():
    port = int(os.environ.get('NO_VNC_PORT', 6080))
    
    with socketserver.TCPServer(("", port), CORSProxy) as httpd:
        print(f"CORS proxy for noVNC running on port {port}")
        httpd.serve_forever()

if __name__ == "__main__":
    main() 