import asyncio
import websockets
import threading
import sys

ESP32_IP = "10.103.215.83"  
ESP32_PORT = 81
URI = f"ws://{ESP32_IP}:{ESP32_PORT}"

websocket_global = None
loop_global = None

async def receive_messages(ws):
    """Terima pesan dari ESP32"""
    try:
        async for message in ws:
            print(f"\r{message}")
            print("[Kamu]: ", end="", flush=True)
    except websockets.exceptions.ConnectionClosed:
        print("\n[INFO] Koneksi terputus dari ESP32.")

def input_thread():
    """Thread terpisah untuk input user"""
    global websocket_global, loop_global
    while True:
        try:
            msg = input("[Kamu]: ")
            if msg.lower() in ("exit", "quit", "q"):
                print("[INFO] Keluar dari chat...")
                asyncio.run_coroutine_threadsafe(
                    websocket_global.close(), loop_global
                )
                sys.exit(0)
            if msg.strip():
                asyncio.run_coroutine_threadsafe(
                    websocket_global.send(f"[Python]: {msg}"),
                    loop_global
                )
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

async def chat():
    global websocket_global, loop_global
    loop_global = asyncio.get_event_loop()

    print(f"[INFO] Menghubungkan ke ESP32 di {URI}...")
    try:
        async with websockets.connect(URI) as ws:
            websocket_global = ws
            print("[INFO] Terhubung! Mulai chat (ketik 'exit' untuk keluar)\n")

            # Jalankan input di thread terpisah
            t = threading.Thread(target=input_thread, daemon=True)
            t.start()

            # Terima pesan
            await receive_messages(ws)
    except ConnectionRefusedError:
        print("[ERROR] Gagal terhubung. Pastikan IP ESP32 benar dan terhubung ke WiFi.")
    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    asyncio.run(chat())