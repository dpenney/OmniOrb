#include "Provisioning.h"
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "waveshare_init.h"

static WebServer server(80);
static DNSServer dnsServer;
static bool portalDone = false;
static ProjectSettings* currentSettings = nullptr;

const char PORTAL_HTML[] = R"rawliteral(
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RADAR SETUP</title>
<style>
body { font-family: 'Courier New', monospace; background: #000; color: #07e0; text-align: center; padding: 20px; }
.container { max-width: 400px; margin: auto; padding: 30px; border: 2px solid #07e; border-radius: 15px; background: #050505; box-shadow: 0 0 20px #07e055; }
input, select { width: 100%; padding: 12px; margin: 12px 0; border: 1px solid #07e0; background: #000; color: #fff; border-radius: 5px; box-sizing: border-box; font-size: 16px; }
button { width: 100%; padding: 15px; background: #03a000; color: #000; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 16px; margin-top: 10px; }
button:active { background: #33a; }
.geo-btn { background: #33a; color: #000; border: 1px solid #07e0; margin-bottom: 20px; }
h2 { letter-spacing: 2px; }
p { font-size: 14px; color: #0350; }
</style>
<script>
function setLocation(sel) {
    var val = sel.value;
    if (val == "manual") {
        document.getElementById('lat').value = "";
        document.getElementById('lon').value = "";
        document.getElementById('gmt').value = "0";
    } else {
        var parts = val.split(",");
        document.getElementById('lat').value = parts[0];
        document.getElementById('lon').value = parts[1];
        document.getElementById('gmt').value = parts[2];
    }
}
</script>
</head><body>
<div class="container">
<h2>COMMAND SETUP</h2>
<p>TRANSMISSION PENDING...</p>
<form action="/save" method="POST">
<input type="text" name="ssid" placeholder="WIFI SSID" required>
<input type="password" name="pass" placeholder="WIFI PASSWORD">
<label style="display:block; text-align:left; font-size:12px; margin-top:10px;">HOME POSITION:</label>
<select onchange="setLocation(this)">
    <option value="33.771524,-92.858774,-6" selected>Barksdale AFB, LA</option>
    <option value="51.681442,-1.802442,0">RAF Fairford</option>
    <option value="37.849993,-122.115747,-7">Bruno's Lair</option>
    <option value="manual">-- MANUAL ENTRY --</option>
</select>
<input type="text" name="lat" id="lat" placeholder="HOME LATITUDE" value="33.771524" required>
<input type="text" name="lon" id="lon" placeholder="HOME LONGITUDE" value="-92.858774" required>
<input type="text" name="gmt" id="gmt" placeholder="GMT OFFSET (e.g. -7)" value="-6" required>
<p style="color: #666; font-size: 12px;">(Use Google Maps or LatLong.net for manual coords)</p>
<button type="submit">COMMIT SETTINGS</button>
</form></div></body></html>
)rawliteral";

void handleRoot() {
    server.send(200, "text/html", PORTAL_HTML);
}

void handleSave() {
    if (currentSettings) {
        strlcpy(currentSettings->wifi_ssid, server.arg("ssid").c_str(), sizeof(currentSettings->wifi_ssid));
        strlcpy(currentSettings->wifi_password, server.arg("pass").c_str(), sizeof(currentSettings->wifi_password));
        currentSettings->home_lat = server.arg("lat").toFloat();
        currentSettings->home_lon = server.arg("lon").toFloat();
        currentSettings->gmt_offset = server.arg("gmt").toFloat();
        
        SettingsManager::save(*currentSettings);
        
        gfx->fillScreen(0x0000);
        gfx->setTextColor(0x07E0);
        gfx->setTextSize(3);
        gfx->setCursor(132, 200); gfx->print("SETTINGS SAVED");
        gfx->setTextSize(2);
        gfx->setCursor(160, 240); gfx->print("REBOOTING...");
        
        server.send(200, "text/html", "Settings saved. Rebooting...");
        delay(2000);
        ESP.restart();
    }
}

void drawPortalStatus() {
    gfx->fillScreen(0x0000);
    uint16_t green = 0x07E0;
    uint16_t dim   = 0x0350;
    
    // Stealth Brackets
    gfx->drawRect(75, 75, 345, 345, green);
    
    // Header
    gfx->setTextColor(green);
    gfx->setTextSize(3);
    gfx->setCursor(128, 100); gfx->print("SETUP BRIEFING");
    
    // SSID Section
    gfx->setTextColor(0xFFFF);
    gfx->setTextSize(2);
    gfx->setCursor(144, 170); gfx->print("1. CONNECT WIFI:");
    gfx->setTextColor(green);
    gfx->setTextSize(3);
    gfx->setCursor(138, 195); gfx->print("RADAR-SETUP");
    
    // URL Section
    gfx->setTextColor(0xFFFF);
    gfx->setTextSize(2);
    gfx->setCursor(144, 270); gfx->print("2. OPEN BROWSER:");
    gfx->setTextColor(green);
    gfx->setTextSize(3);
    gfx->setCursor(138, 295); gfx->print("192.168.4.1");
    
    // Footer
    gfx->setTextColor(dim);
    gfx->setTextSize(2);
    gfx->setCursor(150, 370); gfx->print("LINK PENDING");
}

void ProvisioningManager::startPortal(ProjectSettings &s) {
    currentSettings = &s;
    WiFi.mode(WIFI_AP);
    WiFi.softAP("RADAR-SETUP");
    
    dnsServer.start(53, "*", WiFi.softAPIP());
    
    server.on("/", handleRoot);
    server.on("/save", HTTP_POST, handleSave);
    server.onNotFound(handleRoot);
    server.begin();
    
    Serial.println("Provisioning AP started: RADAR-SETUP");
    drawPortalStatus();
    
    while(!portalDone) {
        dnsServer.processNextRequest();
        server.handleClient();
        delay(10);
    }
}

bool ProvisioningManager::isSetupRequested() {
    return false;
}
