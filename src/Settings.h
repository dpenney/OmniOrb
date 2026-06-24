#ifndef SETTINGS_H
#define SETTINGS_H

#include <Arduino.h>
#include <ArduinoJson.h>

struct ProjectSettings {
    char wifi_ssid[64];
    char wifi_password[64];
    char adsb_host[64];
    float home_lat;
    float home_lon;
    float range_nm;
    float gmt_offset;
    char timezone[64];
    int volume;          // speaker volume 0-100, default 75

    ProjectSettings();
};

class SettingsManager {
public:
    static bool load(ProjectSettings &s);
    static bool save(const ProjectSettings &s);
    static void reset(ProjectSettings &s);
};

#endif
