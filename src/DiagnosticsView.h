#ifndef DIAGNOSTICS_VIEW_H
#define DIAGNOSTICS_VIEW_H

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <FreeRTOS.h>

class DiagnosticsView {
public:
    static void set_canvas(Arduino_Canvas *c);
    static void set_vsync_sem(SemaphoreHandle_t s);
    static void init();
    static void update();
    
    // Diagnostic Data setters
    static void set_mic_intensity(int intensity);
    static void set_oww_status(const char* status);
    static void set_pi_status(const char* status);
    static void set_wifi_rssi(int rssi);
    static void set_last_wake(const char* timestamp);

private:
    static Arduino_Canvas* canvas;
    static SemaphoreHandle_t vsync_sem;
    
    static int mic_intensity;
    static String oww_status;
    static String pi_status;
    static int wifi_rssi;
    static String last_wake;
    static uint32_t last_update_ms;
};

#endif
