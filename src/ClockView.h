#ifndef CLOCKVIEW_H
#define CLOCKVIEW_H

#include <Arduino_GFX_Library.h>

#include <Arduino_GFX.h>
#include <lvgl.h>

class ClockView {
public:
    static void init();
    static void update_time();
    static void show();
    static void hide();
    static void set_timer_pct(int pct);  // -1 = hide, 0-100 = show arc
};

#endif
