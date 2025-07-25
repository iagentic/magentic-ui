# api/app.py
import os
import yaml
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

# import logging
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketState
import httpx
import asyncio
from loguru import logger

from ...version import VERSION
from .config import settings
from .deps import cleanup_managers, init_managers
from .initialization import AppInitializer
from .routes import (
    plans,
    runs,
    sessions,
    settingsroute,
    teams,
    validation,
    ws,
)

# Initialize application
app_file_path = os.path.dirname(os.path.abspath(__file__))
initializer = AppInitializer(settings, app_file_path)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Lifecycle manager for the FastAPI application.
    Handles initialization and cleanup of application resources.
    """

    try:
        # Load the config if provided
        config: dict[str, Any] = {}
        config_file = os.environ.get("_CONFIG")
        if config_file:
            logger.info(f"Loading config from file: {config_file}")
            with open(config_file, "r") as f:
                config = yaml.safe_load(f)
        else:
            logger.info("No config file provided, using defaults.")

        # Initialize managers (DB, Connection, Team)
        await init_managers(
            initializer.database_uri,
            initializer.config_dir,
            initializer.app_root,
            os.environ["INTERNAL_WORKSPACE_ROOT"],
            os.environ["EXTERNAL_WORKSPACE_ROOT"],
            os.environ["INSIDE_DOCKER"] == "1",
            config,
            os.environ["RUN_WITHOUT_DOCKER"] == "True",
        )

        # Any other initialization code
        logger.info(
            f"Application startup complete. Navigate to http://{os.environ.get('_HOST', '127.0.0.1')}:{os.environ.get('_PORT', '8081')}"
        )

    except Exception as e:
        logger.error(f"Failed to initialize application: {str(e)}")
        raise

    yield  # Application runs here

    # Shutdown
    try:
        logger.info("Cleaning up application resources...")
        await cleanup_managers()
        logger.info("Application shutdown complete")
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}")


# Create FastAPI application with increased file size limits
app = FastAPI(
    lifespan=lifespan, 
    debug=True,
    # Increase file upload size limits
    max_request_size=100 * 1024 * 1024,  # 100MB
)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8001",
        "http://localhost:8081",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create API router with version and documentation
api = FastAPI(
    root_path="/api",
    title="Magentic-UI API",
    version=VERSION,
    description="Magentic-UI is an application to interact with web agents.",
    docs_url="/docs" if settings.API_DOCS else None,
)

# Include all routers with their prefixes
api.include_router(
    sessions.router,
    prefix="/sessions",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)

api.include_router(
    plans.router,
    prefix="/plans",
    tags=["plans"],
    responses={404: {"description": "Not found"}},
)

api.include_router(
    runs.router,
    prefix="/runs",
    tags=["runs"],
    responses={404: {"description": "Not found"}},
)

api.include_router(
    teams.router,
    prefix="/teams",
    tags=["teams"],
    responses={404: {"description": "Not found"}},
)


api.include_router(
    ws.router,
    prefix="/ws",
    tags=["websocket"],
    responses={404: {"description": "Not found"}},
)

api.include_router(
    validation.router,
    prefix="/validate",
    tags=["validation"],
    responses={404: {"description": "Not found"}},
)

api.include_router(
    settingsroute.router,
    prefix="/settings",
    tags=["settings"],
    responses={404: {"description": "Not found"}},
)


# Version endpoint


@api.get("/version")
async def get_version():
    """Get API version"""
    return {
        "status": True,
        "message": "Version retrieved successfully",
        "data": {"version": VERSION},
    }


# Health check endpoint


@api.get("/health")
async def health_check():
    """API health check endpoint"""
    return {
        "status": True,
        "message": "Service is healthy",
    }


# Mount static file directories
app.mount("/api", api)
app.mount(
    "/files",
    StaticFiles(directory=initializer.static_root, html=True),
    name="files",
)

# Add VNC proxy route to avoid CORS issues
@app.get("/vnc/{path:path}")
async def vnc_proxy(path: str, request: Request):
    """Proxy VNC requests to avoid CORS issues"""
    # Get VNC port from environment or config, default to 6080
    vnc_port = os.environ.get("NOVNC_PORT", "6080")
    
    # Try to get the actual VNC port from the websocket manager configuration
    try:
        from .deps import get_websocket_manager
        websocket_manager = await get_websocket_manager()
        if websocket_manager and hasattr(websocket_manager, 'config'):
            config_vnc_port = websocket_manager.config.get("novnc_port")
            if config_vnc_port and config_vnc_port != -1:
                vnc_port = str(config_vnc_port)
                logger.info(f"Using VNC port from config: {vnc_port}")
    except Exception as e:
        logger.warning(f"Could not get VNC port from config: {e}")
    
    # Forward the request to the VNC container
    vnc_url = f"http://localhost:{vnc_port}/{path}"
    
    logger.info(f"VNC proxy request: {path} -> {vnc_url}")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(vnc_url, params=request.query_params)
            logger.info(f"VNC proxy response: {response.status_code} for {path}")
            
            # Special handling for vnc.html to modify WebSocket URLs
            if path == "vnc.html":
                content = response.text
                # Replace WebSocket URLs to use our proxy
                host = request.headers.get('host', 'localhost:8081')
                content = content.replace(f'ws://localhost:{vnc_port}/', f'ws://{host}/vnc-ws/')
                content = content.replace(f'wss://localhost:{vnc_port}/', f'wss://{host}/vnc-ws/')
                # Also replace any relative WebSocket paths
                content = content.replace('href="websockify"', f'href="ws://{host}/vnc-ws/websockify"')
                content = content.replace('href="/websockify"', f'href="ws://{host}/vnc-ws/websockify"')
                
                return Response(
                    content=content,
                    status_code=response.status_code,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                        "Content-Type": "text/html",
                        **{k: v for k, v in response.headers.items() if k.lower() not in ['content-length', 'content-type']}
                    }
                )
            else:
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                        **{k: v for k, v in response.headers.items() if k.lower() not in ['content-length']}
                    }
                )
    except Exception as e:
        logger.error(f"VNC proxy error: {e}")
        return Response(content="VNC not available", status_code=503)

# Add WebSocket proxy for VNC WebSocket connections
@app.websocket("/vnc-ws/{path:path}")
async def vnc_websocket_proxy(websocket: WebSocket, path: str):
    await websocket.accept()
    logger.info(f"VNC WebSocket proxy request: {path}")
    try:
        # Get VNC port from environment or config, default to 6080
        vnc_port = os.environ.get("NOVNC_PORT", "6080")
        
        # Try to get the actual VNC port from the websocket manager configuration
        try:
            from .deps import get_websocket_manager
            websocket_manager = await get_websocket_manager()
            if websocket_manager and hasattr(websocket_manager, 'config'):
                config_vnc_port = websocket_manager.config.get("novnc_port")
                if config_vnc_port and config_vnc_port != -1:
                    vnc_port = str(config_vnc_port)
                    logger.info(f"Using VNC port from config: {vnc_port}")
        except Exception as e:
            logger.warning(f"Could not get VNC port from config: {e}")
        
        possible_paths = [
            f"ws://localhost:{vnc_port}/{path}",
            f"ws://localhost:{vnc_port}/websockify",
            f"ws://localhost:{vnc_port}/websockify/",
            f"ws://localhost:{vnc_port}/",
        ]
        import websockets
        vnc_websocket = None
        for vnc_ws_url in possible_paths:
            try:
                logger.info(f"Attempting to connect to: {vnc_ws_url}")
                vnc_websocket = await websockets.connect(vnc_ws_url)
                logger.info(f"Successfully connected to VNC WebSocket: {vnc_ws_url}")
                break
            except Exception as e:
                logger.warning(f"Failed to connect to {vnc_ws_url}: {e}")
                continue
        if vnc_websocket is None:
            logger.error("Failed to connect to any VNC WebSocket URL")
            await websocket.close(code=1011, reason="VNC WebSocket not available")
            return
        async def forward_to_vnc():
            try:
                while True:
                    # Check if the client WebSocket is still connected
                    if websocket.client_state == WebSocketState.DISCONNECTED:
                        logger.info("Client WebSocket disconnected, stopping forward_to_vnc")
                        break
                    
                    msg = await websocket.receive()
                    if "bytes" in msg and msg["bytes"] is not None:
                        await vnc_websocket.send(msg["bytes"])
                    elif "text" in msg and msg["text"] is not None:
                        await vnc_websocket.send(msg["text"])
            except Exception as e:
                logger.error(f"Error forwarding to VNC: {e}")
        async def forward_from_vnc():
            try:
                async for message in vnc_websocket:
                    # Check if the client WebSocket is still connected
                    if websocket.client_state == WebSocketState.DISCONNECTED:
                        logger.info("Client WebSocket disconnected, stopping forward_from_vnc")
                        break
                        
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(str(message))
            except Exception as e:
                logger.error(f"Error forwarding from VNC: {e}")
        await asyncio.gather(forward_to_vnc(), forward_from_vnc(), return_exceptions=True)
        await vnc_websocket.close()
    except WebSocketDisconnect:
        logger.info("VNC WebSocket proxy client disconnected")
    except Exception as e:
        logger.error(f"VNC WebSocket proxy error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal error")
        except:
            pass

# Add direct WebSocket proxy for the path the client is actually using
@app.websocket("/vnc/websockify")
async def vnc_websocket_direct(websocket: WebSocket):
    await websocket.accept()
    logger.info("VNC WebSocket direct proxy request: /vnc/websockify")
    try:
        # Get VNC port from environment or config, default to 6080
        vnc_port = os.environ.get("NOVNC_PORT", "6080")
        
        # Try to get the actual VNC port from the websocket manager configuration
        try:
            from .deps import get_websocket_manager
            websocket_manager = await get_websocket_manager()
            if websocket_manager and hasattr(websocket_manager, 'config'):
                config_vnc_port = websocket_manager.config.get("novnc_port")
                if config_vnc_port and config_vnc_port != -1:
                    vnc_port = str(config_vnc_port)
                    logger.info(f"Using VNC port from config: {vnc_port}")
        except Exception as e:
            logger.warning(f"Could not get VNC port from config: {e}")
        
        vnc_ws_url = f"ws://localhost:{vnc_port}/websockify"
        import websockets
        logger.info(f"Attempting to connect to: {vnc_ws_url}")
        async with websockets.connect(vnc_ws_url) as vnc_websocket:
            logger.info(f"Successfully connected to VNC WebSocket: {vnc_ws_url}")
            async def forward_to_vnc():
                try:
                    while True:
                        # Check if the client WebSocket is still connected
                        if websocket.client_state == WebSocketState.DISCONNECTED:
                            logger.info("Client WebSocket disconnected, stopping forward_to_vnc")
                            break
                        
                        msg = await websocket.receive()
                        if "bytes" in msg and msg["bytes"] is not None:
                            await vnc_websocket.send(msg["bytes"])
                        elif "text" in msg and msg["text"] is not None:
                            await vnc_websocket.send(msg["text"])
                except Exception as e:
                    logger.error(f"Error forwarding to VNC: {e}")
            async def forward_from_vnc():
                try:
                    async for message in vnc_websocket:
                        # Check if the client WebSocket is still connected
                        if websocket.client_state == WebSocketState.DISCONNECTED:
                            logger.info("Client WebSocket disconnected, stopping forward_from_vnc")
                            break
                            
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(str(message))
                except Exception as e:
                    logger.error(f"Error forwarding from VNC: {e}")
            await asyncio.gather(forward_to_vnc(), forward_from_vnc(), return_exceptions=True)
    except WebSocketDisconnect:
        logger.info("VNC WebSocket direct proxy client disconnected")
    except Exception as e:
        logger.error(f"VNC WebSocket direct proxy error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal error")
        except:
            pass

# Mount UI static files - but be more specific to avoid catching WebSocket requests
app.mount("/ui", StaticFiles(directory=initializer.ui_root, html=True), name="ui")

# Catch-all route for UI files (but not WebSocket requests)
@app.get("/{path:path}")
async def ui_catch_all(path: str, request: Request):
    """Serve UI files for paths that don't match other routes"""
    # Skip if this looks like a WebSocket request
    if "websocket" in request.headers.get("upgrade", "").lower():
        raise HTTPException(status_code=404, detail="Not found")
    
    # Try to serve the file from the UI directory
    try:
        file_path = os.path.join(initializer.ui_root, path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        else:
            # If file doesn't exist, serve index.html for SPA routing
            index_path = os.path.join(initializer.ui_root, "index.html")
            if os.path.isfile(index_path):
                return FileResponse(index_path)
            else:
                raise HTTPException(status_code=404, detail="Not found")
    except Exception as e:
        logger.error(f"Error serving UI file {path}: {e}")
        raise HTTPException(status_code=404, detail="Not found")

# Error handlers


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error(f"Internal error: {str(exc)}")
    return {
        "status": False,
        "message": "Internal server error",
        "detail": str(exc) if settings.API_DOCS else "Internal server error",
    }


def create_app() -> FastAPI:
    """
    Factory function to create and configure the FastAPI application.
    Useful for testing and different deployment scenarios.
    """
    return app
