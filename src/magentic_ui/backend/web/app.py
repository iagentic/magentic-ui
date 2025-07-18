# api/app.py
import os
import yaml
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

# import logging
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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


# Create FastAPI application
app = FastAPI(lifespan=lifespan, debug=True)

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
    # Forward the request to the VNC container
    vnc_url = f"http://localhost:6080/{path}"
    
    logger.info(f"VNC proxy request: {path} -> {vnc_url}")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(vnc_url, params=request.query_params)
            logger.info(f"VNC proxy response: {response.status_code} for {path}")
            
            # Special handling for vnc.html to modify WebSocket URLs
            if path == "vnc.html":
                content = response.text
                # Replace WebSocket URLs to use our proxy
                content = content.replace('ws://localhost:6080/', 'ws://' + request.headers.get('host', 'localhost:8081') + '/vnc-ws/')
                content = content.replace('wss://localhost:6080/', 'wss://' + request.headers.get('host', 'localhost:8081') + '/vnc-ws/')
                
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
    """Proxy WebSocket connections for VNC to avoid CORS issues"""
    await websocket.accept()
    
    logger.info(f"VNC WebSocket proxy request: {path}")
    
    try:
        # Connect to the VNC WebSocket
        vnc_ws_url = f"ws://localhost:6080/{path}"
        
        # Use websockets library for proper WebSocket proxying
        import websockets
        
        async with websockets.connect(vnc_ws_url) as vnc_websocket:
            logger.info(f"Connected to VNC WebSocket: {vnc_ws_url}")
            
            # Create tasks for bidirectional forwarding
            async def forward_to_vnc():
                try:
                    async for message in websocket.iter_text():
                        await vnc_websocket.send(message)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding to VNC: {e}")
            
            async def forward_from_vnc():
                try:
                    async for message in vnc_websocket:
                        await websocket.send_text(message)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding from VNC: {e}")
            
            # Run both forwarding tasks
            await asyncio.gather(
                forward_to_vnc(),
                forward_from_vnc(),
                return_exceptions=True
            )
        
    except WebSocketDisconnect:
        logger.info("VNC WebSocket proxy client disconnected")
    except Exception as e:
        logger.error(f"VNC WebSocket proxy error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal error")
        except:
            pass

app.mount("/", StaticFiles(directory=initializer.ui_root, html=True), name="ui")

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
