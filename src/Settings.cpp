#include "Settings.h"
#include <LittleFS.h>
#include "config.h"

ProjectSettings::ProjectSettings() {
    strlcpy(wifi_ssid, WIFI_SSID, sizeof(wifi_ssid));
    strlcpy(wifi_password, WIFI_PASSWORD, sizeof(wifi_password));
    strlcpy(adsb_host, ADSB_HOST, sizeof(adsb_host));
    home_lat = HOME_LAT;
    home_lon = HOME_LON;
    range_nm = DEFAULT_RANGE_NM;
    gmt_offset = DEFAULT_GMT_OFFSET;
    strlcpy(timezone, DEFAULT_TIMEZONE, sizeof(timezone));
    volume = 75;
}

void SettingsManager::reset(ProjectSettings &s) {
    s = ProjectSettings();
}

bool SettingsManager::load(ProjectSettings &s) {
    if (!LittleFS.begin(true)) {
        Serial.println("LittleFS mount failed");
        return false;
    }

    if (!LittleFS.exists("/config.json")) {
        Serial.println("No config file found");
        return false;
    }

    File f = LittleFS.open("/config.json", "r");
    if (!f) return false;

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, f);
    f.close();

    if (err) {
        Serial.println("JSON Parse failed");
        return false;
    }

    strlcpy(s.wifi_ssid, doc["wifi_ssid"] | "", sizeof(s.wifi_ssid));
    strlcpy(s.wifi_password, doc["wifi_password"] | "", sizeof(s.wifi_password));
    s.home_lat = doc["home_lat"] | 0.0f;
    s.home_lon = doc["home_lon"] | 0.0f;
    s.range_nm = doc["range_nm"] | 15.0f;
    s.gmt_offset = doc["gmt_offset"] | 0.0f;
    strlcpy(s.timezone, doc["timezone"] | DEFAULT_TIMEZONE, sizeof(s.timezone));
    s.volume = doc["volume"] | 75;

    // Migrate or load adsb_host
    strlcpy(s.adsb_host, doc["adsb_host"] | "", sizeof(s.adsb_host));
    if (strlen(s.adsb_host) == 0) {
        strlcpy(s.adsb_host, ADSB_HOST, sizeof(s.adsb_host));
        SettingsManager::save(s);
    }

    // Migration: if GMT offset is 0 but we're in the Pacific US, it was never set.
    // Auto-correct to the compiled default and re-save so the fix persists.
    if (s.gmt_offset == 0.0f && s.home_lon < -100.0f) {
        Serial.printf("GMT offset migration: applying DEFAULT_GMT_OFFSET (%d)\n", DEFAULT_GMT_OFFSET);
        s.gmt_offset = (float)DEFAULT_GMT_OFFSET;
        SettingsManager::save(s);
    }

    return true;
}

bool SettingsManager::save(const ProjectSettings &s) {
    if (!LittleFS.begin(true)) return false;

    File f = LittleFS.open("/config.json", "w");
    if (!f) return false;

    JsonDocument doc;
    doc["wifi_ssid"] = s.wifi_ssid;
    doc["wifi_password"] = s.wifi_password;
    doc["adsb_host"] = s.adsb_host;
    doc["home_lat"] = s.home_lat;
    doc["home_lon"] = s.home_lon;
    doc["range_nm"] = s.range_nm;
    doc["gmt_offset"] = s.gmt_offset;
    doc["timezone"] = s.timezone;
    doc["volume"]   = s.volume;

    serializeJson(doc, f);
    f.close();
    return true;
}
