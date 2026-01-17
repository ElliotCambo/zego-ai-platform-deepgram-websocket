# Deepgram WebSocket Proxy Service

This service provides a WebSocket interface for real-time audio streaming transcription using Deepgram's API. **Optimized for telephony integration** (Talkdesk, Twilio, etc.) with support for continuous streaming, telephony audio formats, and connection tracking.

## Features

- ✅ Real-time audio streaming transcription
- ✅ Telephony-optimized (continuous streams, silence handling)
- ✅ Support for telephony audio formats (PCM, μ-law, A-law)
- ✅ Connection tracking and monitoring
- ✅ Interim and final transcriptions
- ✅ Full Deepgram response forwarding

See [TELEPHONY_INTEGRATION.md](./TELEPHONY_INTEGRATION.md) for detailed telephony integration guide.

## Features

- Real-time audio streaming transcription
- WebSocket-based bidirectional communication
- Supports interim and final transcription results
- Low-latency streaming with chunked audio input

## Usage

### Start the Service

```bash
docker-compose up -d deepgram-websocket
```

### WebSocket Endpoint

**URL:** `ws://localhost:4002/ws/transcribe`

### Message Format

#### Client → Server
- Send binary audio chunks (WAV, MP3, PCM, etc.)
- Send empty chunk to signal end of stream

#### Server → Client
JSON messages with the following structure:

```json
{
  "type": "transcript",
  "text": "transcribed text",
  "is_final": false,
  "confidence": 0.95
}
```

Message types:
- `ready` - Connection established, ready to receive audio
- `connected` - Deepgram connection established
- `transcript` - Transcription result (interim or final)
- `error` - Error occurred
- `closed` - Connection closed

### Python Example

```python
import asyncio
import websockets
import json

async def transcribe_audio(audio_file_path):
    uri = "ws://localhost:4002/ws/transcribe"
    
    async with websockets.connect(uri) as websocket:
        # Wait for ready
        ready = await websocket.recv()
        print(f"Ready: {ready}")
        
        # Send audio chunks
        with open(audio_file_path, 'rb') as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                await websocket.send(chunk)
                await asyncio.sleep(0.1)
        
        # Receive transcriptions
        while True:
            message = await websocket.recv()
            data = json.loads(message)
            
            if data["type"] == "transcript":
                prefix = "[FINAL]" if data["is_final"] else "[INTERIM]"
                print(f"{prefix} {data['text']}")
                
                if data["is_final"]:
                    break
```

### Test Script

Use the provided test script:

```bash
python3 test_files/test_websocket_client.py test_files/audio.wav
```

## Configuration

- **Port:** 4002 (configurable via `PORT` env var)
- **Model:** nova-2 (Deepgram's latest model)
- **Language:** English (configurable in code)
- **Interim Results:** Enabled (for real-time feedback)

## Health Check

```bash
curl http://localhost:4002/health
```

