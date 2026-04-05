#ifndef ASSISTANTVIEW_H
#define ASSISTANTVIEW_H

#include <Arduino_GFX_Library.h>

class AssistantView {
public:
    enum AssistantState { STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_SPEAKING };

    static void init();
    static void update();
    static void show();
    static void hide();
    static void set_canvas(Arduino_Canvas *canvas);
    static void set_audio_intensity(int intensity);
    static void set_spectrum(const int* bins, int count);
    static void set_state(AssistantState state);

    // Timer — managed by main.cpp
    static void  start_timer(uint32_t seconds, const String& label);
    static void  clear_timer();
    static void  tick_timer();          // Call every loop to advance state (no drawing)
    static bool  is_timer_done();       // True once after expiry; cleared by clear_timer()
    static bool  is_timer_active();     // True while counting down
    static float get_timer_vis_pct();   // Smoothed 0-100 value for rendering
    static const String& timer_label();

private:
    static int freq_bins[16];
};

#endif
