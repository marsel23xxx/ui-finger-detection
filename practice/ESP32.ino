#include <WiFi.h>
#include <WebSocketsServer.h>

const char* ssid     = "marcell";
const char* password = "01010101";

WebSocketsServer webSocket = WebSocketsServer(81);
uint8_t connectedClient = 255;

void webSocketEvent(uint8_t num, WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    case WStype_DISCONNECTED:
      Serial.printf("[WS] Client #%u disconnected\n", num);
      connectedClient = 255;
      break;

    case WStype_CONNECTED: {
      IPAddress ip = webSocket.remoteIP(num);
      Serial.printf("[WS] Client #%u connected from %s\n", num, ip.toString().c_str());
      connectedClient = num;
      webSocket.sendTXT(num, "Halo dari ESP32! Ketik pesan untuk chat.");
      break;
    }

    case WStype_TEXT:
      Serial.printf("[Python]: %s\n", payload);
      break;

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  delay(100);
  WiFi.begin(ssid, password); 

  Serial.print("Connecting to WiFi");
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 30) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nGagal konek! Status: " + String(WiFi.status()));
    return; // stop setup
  }

  Serial.println("\nWiFi connected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP()); // ← catat IP ini untuk Python

  webSocket.begin();
  webSocket.onEvent(webSocketEvent);
  Serial.println("WebSocket server started on port 81");
}

void loop() {
  webSocket.loop();

  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    msg.trim();
    if (msg.length() > 0 && connectedClient != 255) {
      Serial.printf("[ESP32 → Python]: %s\n", msg.c_str());
      webSocket.sendTXT(connectedClient, "[ESP32]: " + msg);
    }
  }
}