#ifndef SETTINGSVIEW_H
#define SETTINGSVIEW_H

#include <Arduino_GFX_Library.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

class SettingsView {
public:
    static void set_canvas(Arduino_Canvas *canvas);
    static void set_gfx(Arduino_GFX *gfx);           // direct panel target for incremental updates
    static void set_vsync_sem(SemaphoreHandle_t sem); // wait for VSYNC before flush
    static void init(int initial_volume);
    static void show();
    static void hide();
    static void update();
    static void set_volume(int v);     // set absolute value, clamp 0-100
    static void adjust_volume(int d);  // relative adjust, clamp 0-100
    static int  get_volume();
    static void set_pi_rssi(int rssi);
    // Returns volume (0-100) for a touch position mapped to arc angle.
    // Positions in the gap (bottom dead zone) are clamped to the nearest end.
    static int  touch_to_volume(int tx, int ty);
};

#endif
