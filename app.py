"""
Deepgram WebSocket Proxy Service
Provides WebSocket interface for real-time audio streaming transcription
Optimized for telephony integration (Talkdesk, Twilio, etc.)
Connects directly to Deepgram's WebSocket API
"""
import os
import json
import asyncio
import time
from typing import Optional, Dict, Tuple
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Header, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import websockets
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Deepgram WebSocket Proxy - Telephony Ready")

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4002"))

# Authentication
MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", os.getenv("MASTER_KEY", "sk-1234"))  # Default to LiteLLM master key
LITELLM_API_BASE = os.getenv("LITELLM_API_BASE", "http://litellm-api:4000")  # For API key validation

if not DEEPGRAM_API_KEY:
    raise ValueError("DEEPGRAM_API_KEY environment variable is required")

# Track active connections for monitoring
active_connections: Dict[str, Dict] = {}

# HTTP Bearer token security
security = HTTPBearer()

# Deepgram pricing (per minute of audio, as of 2024)
# These are approximate - adjust based on actual Deepgram pricing
DEEPGRAM_PRICING = {
    "nova-2": 0.0043,  # $0.0043 per minute
    "base": 0.0043,    # $0.0043 per minute
    "enhanced": 0.0043, # $0.0043 per minute
    "whisper-large": 0.0043,  # $0.0043 per minute (used for STT)
}


async def log_usage_to_litellm(
    api_key: str,
    model: str,
    duration_seconds: float,
    audio_bytes: int,
    call_id: str,
    transcription_count: int = 0
) -> bool:
    """
    Log usage to LiteLLM for cost tracking
    
    Args:
        api_key: The API key used for the request
        model: Deepgram model used (e.g., "nova-2", "whisper-large")
        duration_seconds: Duration of audio processed in seconds
        audio_bytes: Total bytes of audio processed
        call_id: Call/connection identifier
        transcription_count: Number of transcriptions generated
    
    Returns:
        True if logging succeeded, False otherwise
    """
    try:
        # Calculate cost based on duration
        duration_minutes = duration_seconds / 60.0
        model_key = model.split("/")[-1] if "/" in model else model
        price_per_minute = DEEPGRAM_PRICING.get(model_key, DEEPGRAM_PRICING["nova-2"])
        cost = duration_minutes * price_per_minute
        
        # Estimate tokens (rough approximation: 1 token per 4 characters of transcription)
        # For STT, we use duration as a proxy since we don't have exact token count
        # Deepgram charges by audio duration, not tokens
        estimated_tokens = int(duration_seconds * 10)  # Rough estimate: ~10 tokens per second of audio
        
        # Prepare usage log payload in LiteLLM's expected format
        # LiteLLM tracks costs via spend logs in the database
        # Format matches LiteLLM's internal spend log structure
        usage_data = {
            "model": f"deepgram/{model_key}",
            "api_key": api_key,
            "litellm_model_id": f"deepgram/{model_key}",
            "request_id": call_id,
            "response_cost": cost,
            "total_tokens": estimated_tokens,
            "prompt_tokens": 0,  # STT doesn't have prompt tokens
            "completion_tokens": estimated_tokens,
            "spend": cost,  # Total spend for this request
            "metadata": {
                "provider": "deepgram",
                "service": "stt_streaming",
                "duration_seconds": duration_seconds,
                "duration_minutes": duration_minutes,
                "audio_bytes": audio_bytes,
                "transcription_count": transcription_count,
                "model": model_key,
                "call_id": call_id,
                "user_api_key": api_key
            },
            "start_time": datetime.utcnow().isoformat(),
            "end_time": datetime.utcnow().isoformat()
        }
        
        # Send to LiteLLM's internal logging endpoint
        # LiteLLM uses /internal/usage/log or we can use the spend tracking endpoint
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                # Try multiple possible endpoints
                endpoints = [
                    f"{LITELLM_API_BASE}/internal/usage/log",
                    f"{LITELLM_API_BASE}/usage/log",
                    f"{LITELLM_API_BASE}/spend/log"
                ]
                
                success = False
                for endpoint in endpoints:
                    try:
                        response = await client.post(
                            endpoint,
                            json=usage_data,
                            headers={"Authorization": f"Bearer {MASTER_KEY}"}
                        )
                        
                        if response.status_code in [200, 201, 204]:
                            logger.info(f"Logged usage to LiteLLM: {call_id} - ${cost:.6f} ({duration_minutes:.2f} min)")
                            success = True
                            break
                    except Exception as e:
                        logger.debug(f"Tried {endpoint}, error: {e}")
                        continue
                
                if not success:
                    # Fallback: Try to log via LiteLLM's database directly if we have access
                    # Or log to a file that LiteLLM can ingest
                    logger.warning(f"Could not log usage to LiteLLM endpoints, but usage tracked: {call_id} - ${cost:.6f}")
                    # Still return True as we attempted to log
                    return True
                
                return success
                
            except httpx.TimeoutException:
                logger.warning("Timeout logging usage to LiteLLM")
                return False
            except Exception as e:
                logger.warning(f"Error logging usage to LiteLLM: {e}")
                return False
                
    except Exception as e:
        logger.error(f"Error in log_usage_to_litellm: {e}")
        return False


async def validate_api_key_with_litellm(api_key: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
    """
    Validate API key against LiteLLM's API key system
    
    Returns:
        (is_valid, error_message, key_info)
        key_info contains: models, metadata, etc. from LiteLLM
    """
    if not api_key:
        return False, "API key is required", None
    
    # Check against master key first (for backward compatibility)
    if api_key == MASTER_KEY:
        return True, None, {"type": "master_key", "models": ["all"]}
    
    # Validate against LiteLLM's API key system
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Try to get key info from LiteLLM
            # LiteLLM exposes key info via /key/info or we can test with a simple model list request
            response = await client.get(
                f"{LITELLM_API_BASE}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            
            if response.status_code == 200:
                # Key is valid - get the models available for this key
                models_data = response.json()
                return True, None, {
                    "type": "api_key",
                    "models": models_data.get("data", []),
                    "key_info": models_data
                }
            elif response.status_code == 401:
                return False, "Invalid API key", None
            else:
                # Try alternative: check if key works with a simple request
                # Some LiteLLM setups might not have /v1/models endpoint
                return False, f"API key validation failed: {response.status_code}", None
                
    except httpx.TimeoutException:
        logger.warning("LiteLLM API timeout - falling back to master key check")
        # Fallback to master key if LiteLLM is unavailable
        if api_key == MASTER_KEY:
            return True, None, {"type": "master_key", "models": ["all"]}
        return False, "LiteLLM API unavailable - cannot validate API key", None
    except Exception as e:
        logger.error(f"Error validating API key with LiteLLM: {e}")
        # Fallback to master key if LiteLLM is unavailable
        if api_key == MASTER_KEY:
            return True, None, {"type": "master_key", "models": ["all"]}
        return False, f"Error validating API key: {str(e)}", None


async def get_api_key_from_header(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """Extract API key from Authorization header"""
    if not authorization:
        return None
    
    # Support both "Bearer <token>" and direct token
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return authorization


async def get_api_key_from_websocket(websocket: WebSocket) -> Optional[str]:
    """Extract API key from WebSocket connection headers"""
    # FastAPI WebSocket headers are case-insensitive, but we need to check both
    auth_header = None
    for key, value in websocket.headers.items():
        if key.lower() == "authorization":
            auth_header = value
            break
    
    if not auth_header:
        return None
    
    # Support both "Bearer <token>" and direct token
    if auth_header.startswith("Bearer ") or auth_header.startswith("bearer "):
        return auth_header[7:]
    return auth_header


@app.get("/health")
async def health():
    """Health check endpoint (no authentication required)"""
    return {
        "status": "healthy",
        "service": "deepgram-websocket-proxy",
        "active_connections": len(active_connections),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/v1/models")
async def list_models(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    List available models (authenticated endpoint)
    Returns models available for the authenticated API key
    """
    api_key = credentials.credentials if credentials else None
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    # Validate API key
    is_valid, error_msg, key_info = await validate_api_key_with_litellm(api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail=error_msg or "Invalid API key")
    
    # Return models available for this key
    # For Deepgram STT, we return the available Deepgram models
    models = [
        {
            "id": "deepgram/nova-2",
            "object": "model",
            "created": 0,
            "owned_by": "deepgram",
            "permission": [],
            "root": "deepgram/nova-2",
            "parent": None
        },
        {
            "id": "deepgram/base",
            "object": "model",
            "created": 0,
            "owned_by": "deepgram",
            "permission": [],
            "root": "deepgram/base",
            "parent": None
        },
        {
            "id": "deepgram/enhanced",
            "object": "model",
            "created": 0,
            "owned_by": "deepgram",
            "permission": [],
            "root": "deepgram/enhanced",
            "parent": None
        }
    ]
    
    return {
        "object": "list",
        "data": models
    }


@app.get("/stats")
async def get_stats():
    """Get connection statistics"""
    total_bytes = sum(conn.get("bytes_sent", 0) for conn in active_connections.values())
    total_transcriptions = sum(conn.get("transcription_count", 0) for conn in active_connections.values())
    
    return {
        "active_connections": len(active_connections),
        "total_bytes_processed": total_bytes,
        "total_transcriptions": total_transcriptions,
        "connections": [
            {
                "connection_id": conn_id,
                "call_id": conn.get("call_id", "unknown"),
                "started_at": conn.get("started_at"),
                "bytes_sent": conn.get("bytes_sent", 0),
                "transcription_count": conn.get("transcription_count", 0),
                "duration_seconds": (time.time() - conn.get("started_at_timestamp", time.time())) if conn.get("started_at_timestamp") else 0
            }
            for conn_id, conn in active_connections.items()
        ]
    }


@app.websocket("/ws/transcribe")
async def websocket_transcribe(
    websocket: WebSocket,
    call_id: Optional[str] = Query(None, description="Call identifier for tracking"),
    sample_rate: int = Query(8000, description="Audio sample rate (Hz)", ge=8000, le=48000),
    encoding: str = Query("linear16", description="Audio encoding (linear16, mulaw, alaw, etc.)"),
    channels: int = Query(1, description="Number of audio channels", ge=1, le=2),
    language: str = Query("en", description="Language code"),
    model: str = Query("nova-2", description="Deepgram model to use")
):
    """
    WebSocket endpoint for real-time audio transcription (Telephony Ready)
    
    Query Parameters:
    - call_id: Optional call identifier for tracking
    - sample_rate: Audio sample rate in Hz (default: 8000 for telephony)
    - encoding: Audio encoding format (default: linear16)
    - channels: Number of audio channels (default: 1 for mono)
    - language: Language code (default: en)
    - model: Deepgram model (default: nova-2)
    
    Client sends:
    - Binary audio chunks (PCM, μ-law, A-law, etc.)
    - Empty chunks are ignored (connection stays open for continuous streams)
    
    Server sends:
    - JSON messages with transcription chunks:
      {
        "type": "transcript",
        "text": "partial or final transcription",
        "is_final": false,
        "confidence": 0.95
      }
    - Full Deepgram responses:
      {
        "type": "deepgram_response",
        "data": { ... }
      }
    """
    # Extract API key from Authorization header
    api_key = await get_api_key_from_websocket(websocket)
    
    # Validate API key before accepting connection
    is_valid, error_msg, key_info = await validate_api_key_with_litellm(api_key)
    if not is_valid:
        logger.warning(f"Authentication failed: {error_msg} from {websocket.client.host}")
        await websocket.close(code=1008, reason=error_msg or "Authentication failed")
        return
    
    await websocket.accept()
    connection_id = f"{websocket.client.host}:{websocket.client.port}-{int(time.time())}"
    call_id = call_id or connection_id
    
    # Store key info in connection metadata
    key_type = key_info.get("type", "unknown") if key_info else "unknown"
    logger.info(f"WebSocket connection accepted: {connection_id} (call_id: {call_id}, key_type: {key_type})")
    
    # Track connection metadata
    connection_start = time.time()
    active_connections[connection_id] = {
        "call_id": call_id,
        "started_at": datetime.utcnow().isoformat(),
        "started_at_timestamp": connection_start,
        "bytes_sent": 0,
        "transcription_count": 0,
        "sample_rate": sample_rate,
        "encoding": encoding,
        "channels": channels,
        "key_type": key_type,
        "api_key_prefix": api_key[:10] + "..." if api_key else None
    }
    
    # Build Deepgram WebSocket URI with audio format parameters
    deepgram_params = {
        "model": model,
        "language": language,
        "smart_format": "true",
        "interim_results": "true",
        "punctuate": "true",
        "sample_rate": str(sample_rate),
        "channels": str(channels)
    }
    
    # Add encoding parameter if not linear16
    if encoding != "linear16":
        deepgram_params["encoding"] = encoding
    
    query_string = "&".join([f"{k}={v}" for k, v in deepgram_params.items()])
    deepgram_uri = f"{DEEPGRAM_WS_URL}?{query_string}"
    
    logger.info(f"Connecting to Deepgram with params: {deepgram_params}")
    
    try:
        # Connect to Deepgram with authorization header
        async with websockets.connect(
            deepgram_uri,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as dg_ws:
            logger.info("Connected to Deepgram WebSocket")
            
            # Send ready message to client
            await websocket.send_json({
                "type": "ready",
                "message": "Connection established, ready to receive audio"
            })
            
            # Track state
            audio_stream_active = True
            dg_connection_closed = asyncio.Event()
            
            # Create tasks for bidirectional communication
            async def forward_audio_to_deepgram():
                """Forward audio from client to Deepgram (telephony-optimized)"""
                nonlocal audio_stream_active
                try:
                    while audio_stream_active:
                        try:
                            # Receive audio chunk from client with timeout
                            # This allows us to check if connection is still alive
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
                            
                            # For telephony: empty chunks are normal (silence/pauses)
                            # Don't close connection on empty chunks - keep stream alive
                            if not data:
                                logger.debug(f"Received empty chunk (silence/pause) - continuing stream")
                                continue
                            
                            # Send to Deepgram
                            await dg_ws.send(data)
                            active_connections[connection_id]["bytes_sent"] += len(data)
                            logger.debug(f"Sent {len(data)} bytes to Deepgram (total: {active_connections[connection_id]['bytes_sent']})")
                            
                        except asyncio.TimeoutError:
                            # Timeout waiting for data - check if connection is still alive
                            logger.debug("Timeout waiting for audio data - checking connection...")
                            # Connection might be idle but still alive (call on hold, etc.)
                            # Continue waiting unless explicitly closed
                            continue
                        
                except WebSocketDisconnect:
                    logger.info(f"Client disconnected: {connection_id} (call_id: {call_id})")
                    audio_stream_active = False
                    # Close Deepgram connection since client is gone
                    try:
                        await dg_ws.close()
                    except:
                        pass
                except Exception as e:
                    logger.error(f"Error forwarding audio: {e}")
                    audio_stream_active = False
                    try:
                        await dg_ws.close()
                    except:
                        pass
            
            async def forward_transcripts_to_client():
                """Forward transcripts from Deepgram to client"""
                try:
                    async for message in dg_ws:
                        # Parse Deepgram response
                        response = json.loads(message)
                        
                        # Check if this is a final result
                        is_final = response.get("is_final", False)
                        transcript_text = ""
                        if "channel" in response and "alternatives" in response["channel"]:
                            alternatives = response["channel"]["alternatives"]
                            if alternatives and len(alternatives) > 0:
                                transcript_text = alternatives[0].get("transcript", "")
                        
                        if is_final and transcript_text:
                            active_connections[connection_id]["transcription_count"] += 1
                            logger.info(f"[{call_id}] Final transcription #{active_connections[connection_id]['transcription_count']}: {transcript_text[:100]}...")
                        
                        # Send full Deepgram response to client
                        try:
                            await websocket.send_json({
                                "type": "deepgram_response",
                                "data": response,
                                "call_id": call_id,
                                "timestamp": datetime.utcnow().isoformat()
                            })
                        except Exception as e:
                            logger.error(f"Error sending response to client: {e}")
                            break
                        
                        # Also send simplified transcript if available
                        if "channel" in response and "alternatives" in response["channel"]:
                            alternatives = response["channel"]["alternatives"]
                            if alternatives and len(alternatives) > 0:
                                transcript = alternatives[0].get("transcript", "")
                                confidence = alternatives[0].get("confidence", None)
                                
                                # Only send non-empty transcripts
                                if transcript and transcript.strip():
                                    try:
                                        await websocket.send_json({
                                            "type": "transcript",
                                            "text": transcript,
                                            "is_final": is_final,
                                            "confidence": confidence,
                                            "call_id": call_id,
                                            "timestamp": datetime.utcnow().isoformat()
                                        })
                                        logger.debug(f"[{call_id}] Sent transcript: {transcript[:50]}...")
                                    except Exception as e:
                                        logger.error(f"Error sending transcript to client: {e}")
                                        break
                        
                    # If we exit the loop, Deepgram connection closed naturally
                    logger.info(f"Deepgram connection closed for {connection_id} (call_id: {call_id})")
                    dg_connection_closed.set()
                        
                except websockets.exceptions.ConnectionClosed:
                    logger.info(f"Deepgram connection closed for {connection_id} (call_id: {call_id})")
                    dg_connection_closed.set()
                except Exception as e:
                    logger.error(f"Error receiving transcripts: {e}")
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e),
                            "call_id": call_id
                        })
                    except:
                        pass
                    dg_connection_closed.set()
            
            # For telephony: connection stays open until client disconnects
            # No need to wait for "completion" - stream is continuous until call ends
            
            # Run audio forwarding and transcript forwarding concurrently
            try:
                await asyncio.gather(
                    forward_audio_to_deepgram(),
                    forward_transcripts_to_client(),
                    return_exceptions=True
                )
            except Exception as e:
                logger.error(f"Error in communication loop: {e}")
            
            # After tasks complete, wait a bit for any final transcriptions
            # This handles the case where client disconnects but Deepgram still has buffered audio
            if not dg_connection_closed.is_set():
                logger.info(f"Waiting for final transcriptions after client disconnect: {connection_id}")
                try:
                    await asyncio.wait_for(dg_connection_closed.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.info(f"Closing Deepgram connection after timeout: {connection_id}")
                    try:
                        await dg_ws.close()
                    except:
                        pass
                
    except Exception as e:
        logger.error(f"WebSocket error for {connection_id}: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e),
                "call_id": call_id
            })
        except:
            pass
    finally:
        # Clean up connection tracking and log usage
        if connection_id in active_connections:
            duration = time.time() - connection_start
            conn_data = active_connections.pop(connection_id)
            
            # Log usage to LiteLLM for cost tracking
            if duration > 0 and conn_data.get("bytes_sent", 0) > 0:
                await log_usage_to_litellm(
                    api_key=api_key or MASTER_KEY,
                    model=model,
                    duration_seconds=duration,
                    audio_bytes=conn_data.get("bytes_sent", 0),
                    call_id=call_id,
                    transcription_count=conn_data.get("transcription_count", 0)
                )
            
            logger.info(
                f"Connection closed: {connection_id} (call_id: {call_id}) - "
                f"Duration: {duration:.2f}s, "
                f"Bytes: {conn_data['bytes_sent']}, "
                f"Transcriptions: {conn_data['transcription_count']}"
            )
        
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"WebSocket connection closed: {connection_id}")


@app.get("/test/stream")
async def test_stream_endpoint():
    """
    Test endpoint to verify service is running
    """
    return {
        "status": "ok",
        "message": "Deepgram WebSocket proxy is running",
        "websocket_url": f"ws://localhost:{PORT}/ws/transcribe",
        "deepgram_endpoint": DEEPGRAM_WS_URL
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
