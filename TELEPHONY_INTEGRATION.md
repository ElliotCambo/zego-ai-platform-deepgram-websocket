# Telephony Integration Guide

This service is optimized for real-time telephony audio transcription, supporting integration with platforms like Talkdesk, Twilio, and other telephony providers.

## Features

- **Continuous Streaming**: Handles long-lived connections for entire call duration
- **Real-time Transcription**: Provides interim and final transcriptions as speech is detected
- **Telephony Audio Formats**: Supports common telephony codecs (PCM, μ-law, A-law)
- **Connection Tracking**: Monitors active calls with metadata (call ID, duration, statistics)
- **Silence Handling**: Properly handles silence/pauses without closing connections

## WebSocket Endpoint

```
ws://your-server:4002/ws/transcribe
```

## Authentication

**⚠️ API Key Required**: All connections require authentication using an API key.

The service accepts the same API keys as LiteLLM:
- **Master Key**: Use `sk-1234` (or your configured master key)
- **Generated Keys**: Use API keys generated in LiteLLM UI

**Security Best Practice**: Pass the API key in the `Authorization` header (not in URL):
```
Authorization: Bearer sk-1234
```

This prevents API keys from appearing in logs, browser history, or server access logs.

## Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `call_id` | string | No | auto-generated | Call identifier for tracking |
| `sample_rate` | integer | No | 8000 | Audio sample rate in Hz (8000 for telephony, 16000 for HD) |
| `encoding` | string | No | linear16 | Audio encoding format (linear16, mulaw, alaw) |
| `channels` | integer | No | 1 | Number of audio channels (1 for mono, 2 for stereo) |
| `language` | string | No | en | Language code (en, es, fr, etc.) |
| `model` | string | No | nova-2 | Deepgram model (nova-2, base, enhanced) |

## Example: Talkdesk Integration

### 1. Configure Talkdesk to Stream Audio

In your Talkdesk configuration, set up audio streaming to your WebSocket endpoint with authentication:

**WebSocket URL:**
```
ws://your-server:4002/ws/transcribe?call_id={call_id}&sample_rate=8000&encoding=mulaw&channels=1
```

**Authorization Header:**
```
Authorization: Bearer sk-1234
```

**Important**: 
- Replace `sk-1234` with your actual API key (master key or generated key from LiteLLM UI)
- Always use the Authorization header - never put API keys in URLs

### 2. Connect and Stream Audio

```javascript
const callId = 'call-12345';
const apiKey = 'sk-1234'; // Use your LiteLLM API key

// Create WebSocket with Authorization header
const ws = new WebSocket(
  `ws://your-server:4002/ws/transcribe?call_id=${callId}&sample_rate=8000&encoding=mulaw&channels=1`,
  {
    headers: {
      'Authorization': `Bearer ${apiKey}`
    }
  }
);

ws.onopen = () => {
  console.log('Connected to transcription service');
};

// Stream audio chunks from Talkdesk
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'transcript') {
    console.log(`[${data.is_final ? 'FINAL' : 'INTERIM'}] ${data.text}`);
    // Update UI with transcription
  } else if (data.type === 'deepgram_response') {
    // Full Deepgram response with metadata
    console.log('Full response:', data.data);
  }
};

// Send audio chunks as they arrive from Talkdesk
function sendAudioChunk(audioData) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(audioData);
  }
}
```

### 3. Handle Call End

When the call ends, Talkdesk will close the WebSocket connection. The service will:
- Process any remaining buffered audio
- Send final transcriptions
- Clean up resources automatically

## Audio Format Specifications

### Telephony (G.711)
- **Sample Rate**: 8000 Hz
- **Encoding**: `mulaw` (μ-law) or `alaw` (A-law)
- **Channels**: 1 (mono)
- **Bit Depth**: 8 bits

### HD Audio
- **Sample Rate**: 16000 Hz
- **Encoding**: `linear16` (PCM)
- **Channels**: 1 (mono) or 2 (stereo)
- **Bit Depth**: 16 bits

## Response Format

### Transcript Message
```json
{
  "type": "transcript",
  "text": "Hello, how can I help you?",
  "is_final": true,
  "confidence": 0.95,
  "call_id": "call-12345",
  "timestamp": "2024-01-15T10:30:45.123Z"
}
```

### Full Deepgram Response
```json
{
  "type": "deepgram_response",
  "data": {
    "type": "Results",
    "is_final": true,
    "channel": {
      "alternatives": [{
        "transcript": "Hello, how can I help you?",
        "confidence": 0.95,
        "words": [...]
      }]
    },
    "metadata": {...}
  },
  "call_id": "call-12345",
  "timestamp": "2024-01-15T10:30:45.123Z"
}
```

## Monitoring Endpoints

### Health Check
```
GET /health
```

Returns:
```json
{
  "status": "healthy",
  "service": "deepgram-websocket-proxy",
  "active_connections": 5,
  "timestamp": "2024-01-15T10:30:45.123Z"
}
```

### Connection Statistics
```
GET /stats
```

Returns:
```json
{
  "active_connections": 5,
  "total_bytes_processed": 1234567,
  "total_transcriptions": 234,
  "connections": [
    {
      "connection_id": "192.168.1.1:54321-1234567890",
      "call_id": "call-12345",
      "started_at": "2024-01-15T10:30:00.000Z",
      "bytes_sent": 123456,
      "transcription_count": 45,
      "duration_seconds": 120.5
    }
  ]
}
```

## Best Practices

1. **Call ID Tracking**: Always provide a unique `call_id` for each call to track transcriptions
2. **Audio Format**: Match the audio format parameters to your telephony provider's output
3. **Error Handling**: Implement reconnection logic for network interruptions
4. **Connection Lifecycle**: Let the service handle connection cleanup - don't manually close unless necessary
5. **Monitoring**: Use the `/stats` endpoint to monitor active calls and system health

## Troubleshooting

### Connection Closes Prematurely
- Check that you're not sending empty chunks as "end of stream" signals
- Verify network stability and WebSocket keepalive settings
- Review logs for error messages

### No Transcriptions Received
- Verify audio format parameters match your telephony provider
- Check that audio chunks are being sent continuously
- Ensure Deepgram API key is valid and has sufficient quota

### High Latency
- Use `nova-2` model for best latency/accuracy balance
- Ensure audio chunks are sent in real-time (not batched)
- Check network latency between your server and Deepgram

## Security Considerations

For production deployments:

1. **Authentication**: Add API key authentication to WebSocket connections
2. **Rate Limiting**: Implement rate limiting per client/IP
3. **TLS/SSL**: Use `wss://` for secure WebSocket connections
4. **IP Whitelisting**: Restrict access to known telephony provider IPs
5. **Connection Limits**: Set maximum concurrent connections

## Example: Python Client

```python
import asyncio
import websockets
import json

async def transcribe_call(call_id, audio_stream, api_key="sk-1234"):
    uri = f"ws://your-server:4002/ws/transcribe?call_id={call_id}&sample_rate=8000&encoding=mulaw"
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    async with websockets.connect(uri, extra_headers=headers) as ws:
        # Receive ready message
        ready = await ws.recv()
        print(f"Ready: {json.loads(ready)}")
        
        async def send_audio():
            async for audio_chunk in audio_stream:
                await ws.send(audio_chunk)
        
        async def receive_transcriptions():
            async for message in ws:
                data = json.loads(message)
                if data.get("type") == "transcript":
                    print(f"[{data.get('is_final', False)}] {data.get('text')}")
        
        await asyncio.gather(send_audio(), receive_transcriptions())
```

