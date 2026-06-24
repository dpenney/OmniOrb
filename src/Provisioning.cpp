#include "Provisioning.h"
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "waveshare_init.h"

static WebServer server(80);
static DNSServer dnsServer;
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
var TZ_MAP = {
  "America/New_York":    -5, "America/Chicago":     -6,
  "America/Denver":      -7, "America/Los_Angeles": -8,
  "America/Anchorage":   -9, "Pacific/Honolulu":   -10,
  "America/Sao_Paulo":   -3, "America/Argentina/Buenos_Aires": -3,
  "Europe/London":        0, "Europe/Paris":         1,
  "Europe/Berlin":        1, "Europe/Helsinki":      2,
  "Europe/Athens":        2, "Europe/Moscow":        3,
  "Asia/Dubai":           4, "Asia/Kolkata":       5.5,
  "Asia/Bangkok":         7, "Asia/Shanghai":        8,
  "Asia/Singapore":       8, "Asia/Tokyo":           9,
  "Australia/Perth":      8, "Australia/Sydney":    10,
  "Pacific/Auckland":    12
};
function setLocation(sel) {
    var val = sel.value;
    if (val == "manual") return;
    var parts = val.split(",");
    document.getElementById('lat').value = parts[0];
    document.getElementById('lon').value = parts[1];
    document.getElementById('gmt').value = parts[2];
    if (parts[3]) document.getElementById('tz').value = parts[3];
}
function setTZ(sel) {
    var tz = sel.value;
    if (TZ_MAP.hasOwnProperty(tz)) {
        document.getElementById('gmt').value = TZ_MAP[tz];
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
<input type="text" name="adsb_host" placeholder="ADSB RECEIVER IP (e.g. 192.168.4.205)" required>
<label style="display:block; text-align:left; font-size:12px; margin-top:10px;">HOME POSITION:</label>
<select onchange="setLocation(this)">
    <option value="manual" selected>-- SELECT PRESET --</option>
    <option value="33.771524,-92.858774,-6,America/Chicago">Barksdale AFB, LA</option>
    <option value="51.681442,-1.802442,0,Europe/London">RAF Fairford</option>
    <option value="37.849993,-122.115747,-7,America/Los_Angeles">Bruno's Lair</option>
</select>
<input type="text" name="lat" id="lat" placeholder="HOME LATITUDE" required>
<input type="text" name="lon" id="lon" placeholder="HOME LONGITUDE" required>
<label style="display:block; text-align:left; font-size:12px; margin-top:10px;">TIMEZONE:</label>
<select name="tz" id="tz" onchange="setTZ(this)">
    <optgroup label="Americas">
    <option value="America/New_York">Eastern (UTC-5)</option>
    <option value="America/Chicago">Central (UTC-6)</option>
    <option value="America/Denver">Mountain (UTC-7)</option>
    <option value="America/Los_Angeles" selected>Pacific (UTC-8)</option>
    <option value="America/Anchorage">Alaska (UTC-9)</option>
    <option value="Pacific/Honolulu">Hawaii (UTC-10)</option>
    <option value="America/Sao_Paulo">Sao Paulo (UTC-3)</option>
    <option value="America/Argentina/Buenos_Aires">Buenos Aires (UTC-3)</option>
    </optgroup>
    <optgroup label="Europe">
    <option value="Europe/London">London (UTC+0)</option>
    <option value="Europe/Paris">Paris/Madrid (UTC+1)</option>
    <option value="Europe/Berlin">Berlin/Rome (UTC+1)</option>
    <option value="Europe/Helsinki">Helsinki (UTC+2)</option>
    <option value="Europe/Athens">Athens (UTC+2)</option>
    <option value="Europe/Moscow">Moscow (UTC+3)</option>
    </optgroup>
    <optgroup label="Asia/Pacific">
    <option value="Asia/Dubai">Dubai (UTC+4)</option>
    <option value="Asia/Kolkata">India (UTC+5:30)</option>
    <option value="Asia/Bangkok">Bangkok (UTC+7)</option>
    <option value="Asia/Shanghai">China (UTC+8)</option>
    <option value="Asia/Singapore">Singapore (UTC+8)</option>
    <option value="Asia/Tokyo">Tokyo (UTC+9)</option>
    <option value="Australia/Perth">Perth (UTC+8)</option>
    <option value="Australia/Sydney">Sydney (UTC+10)</option>
    <option value="Pacific/Auckland">Auckland (UTC+12)</option>
    </optgroup>
</select>
<input type="text" name="gmt" id="gmt" placeholder="GMT OFFSET (e.g. -7)" value="-8" required>
<p style="color: #666; font-size: 12px;">(Coords: use Google Maps. GMT offset auto-fills from timezone.)</p>
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
        strlcpy(currentSettings->adsb_host, server.arg("adsb_host").c_str(), sizeof(currentSettings->adsb_host));
        currentSettings->home_lat = server.arg("lat").toFloat();
        currentSettings->home_lon = server.arg("lon").toFloat();
        currentSettings->gmt_offset = server.arg("gmt").toFloat();
        strlcpy(currentSettings->timezone, server.arg("tz").c_str(), sizeof(currentSettings->timezone));
        
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
    
    // The portal only exits via ESP.restart() in handleSave()
    for (;;) {
        dnsServer.processNextRequest();
        server.handleClient();
        delay(10);
    }
}

bool ProvisioningManager::isSetupRequested() {
    return false;
}
