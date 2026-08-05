import asyncio
import websockets
import orjson


async def pumpdev_data_stream():
    uri = "wss://pumpdev.io/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(orjson.dumps({"method": "subscribeNewToken"}))
        async for message in ws:
            event = orjson.loads(message)
            print(event)  # {'action': 'buy', 'pool': 'pump', ...}


# Fallback: If websocket disconnnects, it auto-reconnects with exponential
# backoff. Falls to DexScreener polling only as last resort (never silently fall sleeps)
# Token Detection => <1s
# Wait For DexScreener To Index The Pair And Give Us Liquidity And Volume Data =>  25s
# MAX Entire Scan Window =>  15m
# If no qualifying token is found in that time, the bot starts a new scan cycle
asyncio.run(pumpdev_data_stream())
