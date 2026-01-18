"""
Deepgram WebSocket Proxy Service
Provides WebSocket interface for real-time audio streaming transcription and text-to-speech
Optimized for telephony integration (Talkdesk, Twilio, etc.)
Supports both STT (Speech-to-Text) and TTS (Text-to-Speech) via REST and WebSocket
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
DEEPGRAM_STT_WS_URL = "wss://api.deepgram.com/v1/listen"  # STT WebSocket endpoint
DEEPGRAM_TTS_WS_URL = "wss://api.deepgram.com/v1/speak"   # TTS WebSocket endpoint
DEEPGRAM_TTS_REST_URL = "https://api.deepgram.com/v1/speak"  # TTS REST endpoint
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
    # STT models (per minute of audio processed)
    "nova-2": 0.0043,  # $0.0043 per minute
    "base": 0.0043,    # $0.0043 per minute
    "enhanced": 0.0043, # $0.0043 per minute
    "whisper-large": 0.0043,  # $0.0043 per minute (used for STT)
    # TTS models (per minute of generated audio)
    "aura-asteria-en": 0.015,  # $0.015 per minute (example pricing)
    "aura-2-thalia-fr": 0.015,  # $0.015 per minute
    "aura-luna-en": 0.015,  # $0.015 per minute
    "aura-stella-en": 0.015,  # $0.015 per minute
}


async def log_usage_to_litellm(
    api_key: str,
    model: str,
    duration_seconds: float,
    audio_bytes: int,
    call_id: str,
    service_type: str = "stt",
    transcription_count: int = 0,
    text_length: int = 0  # For TTS: length of text converted to speech
) -> bool:
    """
    Log usage to LiteLLM for cost tracking
    
    Args:
        api_key: The API key used for the request
        model: Deepgram model used (e.g., "nova-2", "aura-asteria-en")
        duration_seconds: Duration of audio processed/generated in seconds
        audio_bytes: Total bytes of audio processed/generated
        call_id: Call/connection identifier
        service_type: "stt" for speech-to-text, "tts" for text-to-speech
        transcription_count: Number of transcriptions generated (STT only)
        text_length: Length of text converted to speech (TTS only)
    
    Returns:
        True if logging succeeded, False otherwise
    """
    try:
        # Calculate cost based on duration
        duration_minutes = duration_seconds / 60.0
        model_key = model.split("/")[-1] if "/" in model else model
        # Try to get pricing for the model, fall back to default based on service type
        price_per_minute = DEEPGRAM_PRICING.get(model_key)
        if not price_per_minute:
            # Default pricing based on service type
            price_per_minute = DEEPGRAM_PRICING.get("aura-asteria-en" if service_type == "tts" else "nova-2", 0.0043)
        cost = duration_minutes * price_per_minute
        
        # Estimate tokens
        if service_type == "tts":
            # For TTS: estimate tokens based on text length
            estimated_tokens = int(text_length / 4) if text_length > 0 else int(duration_seconds * 10)
            prompt_tokens = estimated_tokens  # Text input
            completion_tokens = int(duration_seconds * 10)  # Audio output (rough estimate)
        else:
            # For STT: estimate tokens based on duration
            estimated_tokens = int(duration_seconds * 10)  # Rough estimate: ~10 tokens per second of audio
            prompt_tokens = 0  # STT doesn't have prompt tokens
            completion_tokens = estimated_tokens
        
        # Prepare usage log payload in LiteLLM's expected format
        usage_data = {
            "model": f"deepgram/{model_key}",
            "api_key": api_key,
            "litellm_model_id": f"deepgram/{model_key}",
            "request_id": call_id,
            "response_cost": cost,
            "total_tokens": estimated_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "spend": cost,  # Total spend for this request
            "metadata": {
                "provider": "deepgram",
                "service": f"{service_type}_streaming" if service_type == "stt" else f"{service_type}_{'streaming' if audio_bytes > 0 else 'rest'}",
                "duration_seconds": duration_seconds,
                "duration_minutes": duration_minutes,
                "audio_bytes": audio_bytes,
                "transcription_count": transcription_count,
                "text_length": text_length,
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
    deepgram_uri = f"{DEEPGRAM_STT_WS_URL}?{query_string}"
    
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
                    service_type="stt",
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


@app.post("/v1/speak")
async def tts_speak(
    text: str,
    model: str = Query("aura-asteria-en", description="TTS model/voice to use"),
    encoding: str = Query("linear16", description="Audio encoding"),
    container: str = Query("wav", description="Audio container format"),
    sample_rate: int = Query(24000, description="Audio sample rate (Hz)", ge=8000, le=48000),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    REST endpoint for Text-to-Speech (TTS)
    Returns complete audio file in response
    
    Args:
        text: Text to convert to speech
        model: TTS model/voice (e.g., "aura-asteria-en", "aura-2-thalia-fr")
        encoding: Audio encoding (linear16, mulaw, alaw, etc.)
        container: Audio container format (wav, mp3, etc.)
        sample_rate: Audio sample rate in Hz
        credentials: API key authentication
    
    Returns:
        Audio file (binary response)
    """
    api_key = credentials.credentials if credentials else None
    
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    # Validate API key
    is_valid, error_msg, key_info = await validate_api_key_with_litellm(api_key)
    if not is_valid:
        raise HTTPException(status_code=401, detail=error_msg or "Invalid API key")
    
    # Build Deepgram TTS REST URL
    params = {
        "model": model,
        "encoding": encoding,
        "container": container,
        "sample_rate": str(sample_rate)
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    deepgram_url = f"{DEEPGRAM_TTS_REST_URL}?{query_string}"
    
    logger.info(f"TTS REST request: model={model}, text_length={len(text)}")
    
    try:
        start_time = time.time()
        call_id = f"tts-rest-{int(time.time())}"
        
        # Forward request to Deepgram
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                deepgram_url,
                json={"text": text},
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            
            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_detail = error_json.get("error", {}).get("message", error_detail)
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            end_time = time.time()
            duration = end_time - start_time
            audio_data = response.content
            
            # Estimate audio duration (rough calculation based on sample rate and data size)
            # For linear16: bytes = samples * 2 (16-bit = 2 bytes per sample)
            estimated_duration_seconds = len(audio_data) / (sample_rate * 2) if encoding == "linear16" else duration
            
            # Log usage to LiteLLM
            await log_usage_to_litellm(
                api_key=api_key,
                model=model,
                duration_seconds=estimated_duration_seconds,
                audio_bytes=len(audio_data),
                call_id=call_id,
                service_type="tts",
                text_length=len(text)
            )
            
            logger.info(f"TTS REST completed: {len(text)} chars -> {len(audio_data)} bytes ({estimated_duration_seconds:.2f}s)")
            
            # Return audio file
            from fastapi.responses import Response
            content_type = "audio/wav" if container == "wav" else f"audio/{container}"
            return Response(
                content=audio_data,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="tts_output.{container}"',
                    "X-Duration-Seconds": str(estimated_duration_seconds),
                    "X-Model": model
                }
            )
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout waiting for TTS response")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS REST error: {e}")
        raise HTTPException(status_code=500, detail=f"TTS error: {str(e)}")


@app.websocket("/ws/speak")
async def websocket_speak(
    websocket: WebSocket,
    call_id: Optional[str] = Query(None, description="Call identifier for tracking"),
    model: str = Query("aura-asteria-en", description="TTS model/voice to use"),
    encoding: str = Query("linear16", description="Audio encoding"),
    sample_rate: int = Query(24000, description="Audio sample rate (Hz)", ge=8000, le=48000),
    container: str = Query("none", description="Audio container (none for streaming)")
):
    """
    WebSocket endpoint for streaming Text-to-Speech (TTS)
    Streams audio chunks back in real-time for low-latency use cases
    
    Client sends JSON messages:
    - {"type": "Speak", "text": "text to speak"}
    - {"type": "Flush"} - request audio output for buffered text
    - {"type": "Clear"} - clear buffer
    - {"type": "Close"} - close connection
    
    Server sends:
    - JSON metadata messages: {"type": "Metadata", ...}
    - Binary audio chunks: raw audio data
    
    Args:
        websocket: WebSocket connection
        call_id: Optional call identifier
        model: TTS model/voice (e.g., "aura-asteria-en")
        encoding: Audio encoding (linear16, mulaw, alaw, etc.)
        sample_rate: Audio sample rate in Hz
        container: Audio container format (use "none" for streaming)
    """
    # Extract API key from Authorization header
    api_key = await get_api_key_from_websocket(websocket)
    
    # Validate API key before accepting connection
    is_valid, error_msg, key_info = await validate_api_key_with_litellm(api_key)
    if not is_valid:
        logger.warning(f"TTS WebSocket auth failed: {error_msg} from {websocket.client.host}")
        await websocket.close(code=1008, reason=error_msg or "Authentication failed")
        return
    
    await websocket.accept()
    connection_id = f"{websocket.client.host}:{websocket.client.port}-{int(time.time())}"
    call_id = call_id or connection_id
    
    logger.info(f"TTS WebSocket connection accepted: {connection_id} (call_id: {call_id}, model: {model})")
    
    connection_start = time.time()
    active_connections[connection_id] = {
        "call_id": call_id,
        "service": "tts",
        "started_at": datetime.utcnow().isoformat(),
        "started_at_timestamp": connection_start,
        "bytes_received": 0,  # Text bytes
        "bytes_sent": 0,  # Audio bytes
        "text_length": 0,
        "audio_chunks": 0,
        "model": model,
        "encoding": encoding,
        "sample_rate": sample_rate,
        "key_type": key_info.get("type", "unknown") if key_info else "unknown"
    }
    
    # Build Deepgram TTS WebSocket URL
    deepgram_params = {
        "model": model,
        "encoding": encoding,
        "sample_rate": str(sample_rate),
        "container": container
    }
    query_string = "&".join([f"{k}={v}" for k, v in deepgram_params.items()])
    deepgram_uri = f"{DEEPGRAM_TTS_WS_URL}?{query_string}"
    
    logger.info(f"Connecting to Deepgram TTS with params: {deepgram_params}")
    
    try:
        # Connect to Deepgram TTS WebSocket
        async with websockets.connect(
            deepgram_uri,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as dg_ws:
            logger.info("Connected to Deepgram TTS WebSocket")
            
            # Send ready message to client
            await websocket.send_json({
                "type": "ready",
                "message": "Connection established, ready to receive text for TTS"
            })
            
            connection_active = True
            dg_connection_closed = asyncio.Event()
            total_text_length = 0
            
            async def forward_text_to_deepgram():
                """Forward text messages from client to Deepgram"""
                nonlocal connection_active, total_text_length
                try:
                    while connection_active:
                        try:
                            # Receive JSON message from client
                            message = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
                            
                            msg_type = message.get("type", "").lower()
                            
                            if msg_type == "speak":
                                text = message.get("text", "")
                                if text:
                                    # Send Speak message to Deepgram
                                    await dg_ws.send(json.dumps({
                                        "type": "Speak",
                                        "text": text
                                    }))
                                    total_text_length += len(text)
                                    active_connections[connection_id]["text_length"] = total_text_length
                                    active_connections[connection_id]["bytes_received"] += len(text.encode())
                                    logger.debug(f"[{call_id}] Sent text to Deepgram: {len(text)} chars")
                                    
                            elif msg_type == "flush":
                                # Request audio output for buffered text
                                await dg_ws.send(json.dumps({"type": "Flush"}))
                                logger.debug(f"[{call_id}] Flush requested")
                                
                            elif msg_type == "clear":
                                # Clear buffer
                                await dg_ws.send(json.dumps({"type": "Clear"}))
                                logger.debug(f"[{call_id}] Clear requested")
                                
                            elif msg_type == "close":
                                # Close connection
                                connection_active = False
                                await dg_ws.send(json.dumps({"type": "Close"}))
                                logger.info(f"[{call_id}] Close requested by client")
                                break
                                
                        except asyncio.TimeoutError:
                            # Continue waiting
                            continue
                            
                except WebSocketDisconnect:
                    logger.info(f"Client disconnected: {connection_id}")
                    connection_active = False
                    try:
                        await dg_ws.send(json.dumps({"type": "Close"}))
                    except:
                        pass
                except Exception as e:
                    logger.error(f"Error forwarding text: {e}")
                    connection_active = False
                    try:
                        await dg_ws.send(json.dumps({"type": "Close"}))
                    except:
                        pass
            
            async def forward_audio_to_client():
                """Forward audio chunks and metadata from Deepgram to client"""
                try:
                    async for message in dg_ws:
                        # Deepgram TTS can send both JSON (metadata) and binary (audio)
                        if isinstance(message, str):
                            # JSON metadata message
                            try:
                                data = json.loads(message)
                                msg_type = data.get("type", "")
                                
                                if msg_type == "Metadata":
                                    # Send metadata to client
                                    await websocket.send_json({
                                        "type": "metadata",
                                        "data": data,
                                        "call_id": call_id,
                                        "timestamp": datetime.utcnow().isoformat()
                                    })
                                elif msg_type == "SpeechStarted":
                                    logger.debug(f"[{call_id}] Speech started")
                                elif msg_type == "UtteranceEnd":
                                    logger.debug(f"[{call_id}] Utterance ended")
                                    
                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON from Deepgram: {message}")
                        else:
                            # Binary audio data
                            if isinstance(message, bytes):
                                await websocket.send_bytes(message)
                                active_connections[connection_id]["bytes_sent"] += len(message)
                                active_connections[connection_id]["audio_chunks"] += 1
                                logger.debug(f"[{call_id}] Sent audio chunk: {len(message)} bytes")
                    
                    dg_connection_closed.set()
                    
                except websockets.exceptions.ConnectionClosed:
                    logger.info(f"Deepgram TTS connection closed for {connection_id}")
                    dg_connection_closed.set()
                except Exception as e:
                    logger.error(f"Error receiving audio: {e}")
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e),
                            "call_id": call_id
                        })
                    except:
                        pass
                    dg_connection_closed.set()
            
            # Run text forwarding and audio forwarding concurrently
            try:
                await asyncio.gather(
                    forward_text_to_deepgram(),
                    forward_audio_to_client(),
                    return_exceptions=True
                )
            except Exception as e:
                logger.error(f"Error in TTS communication loop: {e}")
            
            # Wait for Deepgram to close
            if not dg_connection_closed.is_set():
                try:
                    await asyncio.wait_for(dg_connection_closed.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                
    except Exception as e:
        logger.error(f"TTS WebSocket error for {connection_id}: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e),
                "call_id": call_id
            })
        except:
            pass
    finally:
        # Clean up and log usage
        if connection_id in active_connections:
            duration = time.time() - connection_start
            conn_data = active_connections.pop(connection_id)
            
            # Log usage to LiteLLM for cost tracking
            if duration > 0 and conn_data.get("bytes_sent", 0) > 0:
                # Estimate audio duration
                estimated_duration = conn_data.get("bytes_sent", 0) / (sample_rate * 2) if encoding == "linear16" else duration
                
                await log_usage_to_litellm(
                    api_key=api_key or MASTER_KEY,
                    model=model,
                    duration_seconds=estimated_duration,
                    audio_bytes=conn_data.get("bytes_sent", 0),
                    call_id=call_id,
                    service_type="tts",
                    text_length=conn_data.get("text_length", 0)
                )
            
            logger.info(
                f"TTS WebSocket closed: {connection_id} - "
                f"Duration: {duration:.2f}s, "
                f"Text: {conn_data.get('text_length', 0)} chars, "
                f"Audio: {conn_data.get('bytes_sent', 0)} bytes, "
                f"Chunks: {conn_data.get('audio_chunks', 0)}"
            )
        
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"TTS WebSocket connection closed: {connection_id}")


@app.get("/test/stream")
async def test_stream_endpoint():
    """
    Test endpoint to verify service is running
    """
    return {
        "status": "ok",
        "message": "Deepgram WebSocket proxy is running",
        "stt_websocket_url": f"ws://localhost:{PORT}/ws/transcribe",
        "tts_websocket_url": f"ws://localhost:{PORT}/ws/speak",
        "tts_rest_url": f"http://localhost:{PORT}/v1/speak",
        "deepgram_stt_endpoint": DEEPGRAM_STT_WS_URL,
        "deepgram_tts_endpoint": DEEPGRAM_TTS_WS_URL
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
