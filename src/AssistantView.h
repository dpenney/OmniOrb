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

private:
    static int freq_bins[16];
};

#endif
