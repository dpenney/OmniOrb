#include "AssistantView.h"
#include <math.h>

// ─── Constants ─────────────────────────────────────────────────────────────
static const uint16_t C_BG        = 0x0000; // Deep Black
static const uint16_t C_ACCENT    = 0xE651; // Brass Gold
static const uint16_t C_HIGHLIGHT = 0x97F2; // Mint Green
static const uint16_t C_SECONDARY = 0x5AC9; // Secondary Neutral
static const uint16_t C_GLOW      = 0x0120; // Dim Green Glow
static const uint16_t C_MATRIX    = 0x07E0; // Matrix Green

static const int CX = 240;
static const int CY = 240;
static const int SCREEN_RADIUS = 230;

static const float AUDIO_DECAY       = 0.85f;  ///< Per-frame decay for smoothed audio scale
static const float SPECTRUM_DECAY    = 0.88f;  ///< Per-frame decay for each spectrum bin

static Arduino_Canvas *canvas = nullptr;
static unsigned long start_ms = 0;
static int cur_audio_intensity = 0;
static float audio_scale = 0.0f; // Smoothed visual scale
int AssistantView::freq_bins[16] = {0};
static float visual_bins[16] = {0.0f}; // Smoothed visual values

// ─── Timer state ────────────────────────────────────────────────────────────
static bool     timer_active   = false;
static uint32_t timer_start_ms = 0;
static uint32_t timer_dur_ms   = 0;
static String   timer_lbl      = "";
static bool     timer_done_flag = false;  // true for one update() frame after expiry
static float    timer_pct_vis  = 100.0f;  // smoothed visual percent

static AssistantView::AssistantState current_state   = AssistantView::STATE_IDLE;
static AssistantView::Emotion       current_emotion = AssistantView::EMO_NEUTRAL;
static AssistantView::AssistantStyle current_style   = AssistantView::STYLE_FACE;

// ─── Expressive state ──────────────────────────────────────────────────────
static uint32_t next_blink_ms = 0;
static uint32_t blink_end_ms = 0;
static bool     is_blinking    = false;

void AssistantView::set_state(AssistantState state) {
    if (state != current_state) {
        // Clear audio data on state transitions to ensure a clean start/end for animations
        cur_audio_intensity = 0;
        audio_scale = 0.0f;
        for (int i = 0; i < 16; i++) {
            freq_bins[i] = 0;
            visual_bins[i] = 0.0f;
        }
        current_state = state;
    }
}

AssistantView::AssistantState AssistantView::get_state() {
    return current_state;
}

void AssistantView::set_emotion(Emotion emotion) {
    current_emotion = emotion;
}

AssistantView::Emotion AssistantView::get_emotion() {
    return current_emotion;
}

void AssistantView::set_style(AssistantStyle style) {
    current_style = style;
}

AssistantView::AssistantStyle AssistantView::get_style() {
    return current_style;
}

void AssistantView::toggle_style() {
    current_style = (current_style == STYLE_IRIS) ? STYLE_FACE : STYLE_IRIS;
}

void AssistantView::set_canvas(Arduino_Canvas *c) {
    canvas = c;
}

void AssistantView::init() {
    start_ms = millis();
    next_blink_ms = start_ms + 2000;
}

void AssistantView::show() {
}

void AssistantView::hide() {
}

void AssistantView::set_audio_intensity(int intensity) {
    cur_audio_intensity = intensity;
    // Debug: print if significant
    if (intensity > 10) {
        // Serial.printf("Got Intensity: %d\n", intensity);
    }
}

void AssistantView::start_timer(uint32_t seconds, const String& label) {
    timer_start_ms  = millis();
    timer_dur_ms    = seconds * 1000UL;
    timer_lbl       = label;
    timer_active    = true;
    timer_done_flag = false;
    timer_pct_vis   = 100.0f;
    // draw_pct (static in update()) will snap on first draw — acceptable
}

void AssistantView::clear_timer() {
    timer_active    = false;
    timer_done_flag = false;
    timer_pct_vis   = -1.0f;
}

void AssistantView::tick_timer() {
    if (!timer_active) return;
    unsigned long elapsed = millis() - timer_start_ms;
    if (elapsed < timer_dur_ms) {
        timer_pct_vis = 100.0f * (1.0f - (float)elapsed / (float)timer_dur_ms);
    } else {
        timer_pct_vis = 0.0f;
        if (!timer_done_flag) {
            timer_done_flag = true;   // fire once; main.cpp will call clear_timer()
        }
    }
}

bool AssistantView::is_timer_done() {
    return timer_done_flag;
}

bool AssistantView::is_timer_active() {
    return timer_active;
}

float AssistantView::get_timer_vis_pct() {
    return timer_pct_vis;
}

const String& AssistantView::timer_label() {
    return timer_lbl;
}

// Blend green→yellow→red based on percent remaining (100=green, 0=red)
static uint16_t timer_ring_color(int pct) {
    if (pct >= 50) {
        float t = (pct - 50) / 50.0f;          // 0=yellow, 1=green
        uint8_t r = (uint8_t)(31 * (1.0f - t));
        return (r << 11) | (63 << 5);          // varying R, full G, no B
    } else {
        float t = pct / 50.0f;                  // 0=red, 1=yellow
        uint8_t g = (uint8_t)(63 * t);
        return (31 << 11) | (g << 5);          // full R, varying G, no B
    }
}

void AssistantView::set_spectrum(const int* bins, int count) {
    for (int i = 0; i < 16 && i < count; i++) {
        freq_bins[i] = bins[i];
    }
}

void AssistantView::_draw_iris(float t) {
    // Original Iris logic
    float base_breath = (sinf(t * 2.0f) + 1.0f) * 0.5f; 
    float breath = base_breath + (audio_scale * 3.0f);
    int iris_r = 60 + (int)(breath * 25);
    
    uint16_t eff_glow = C_GLOW;
    uint16_t eff_accent = C_ACCENT;
    uint16_t eff_high = C_HIGHLIGHT;
    
    if (current_state == AssistantView::STATE_LISTENING) {
        eff_glow = 0xF800; // Red
        eff_accent = 0xFC00; // Orange
        eff_high = 0xFFFF; // White
    } else if (current_state == AssistantView::STATE_THINKING) {
        eff_glow = 0x001F; // Blue
        eff_accent = 0x07FF; // Cyan
    } else if (current_state == AssistantView::STATE_SPEAKING) {
        eff_glow = 0xF81F; // Magenta
        eff_accent = 0x780F; // Purple
    } else if (current_state == AssistantView::STATE_CONTINUITY) {
        eff_glow = 0x07E0; // Web Green (Matches LISTENING)
        eff_accent = 0x00FF; // Deep Blue (Matches LISTENING)
        
        // Match the listening state animation floor
        if (audio_scale < 0.1f) audio_scale = 0.1f;
    }

    canvas->fillCircle(CX, CY, iris_r + 15 + (int)(audio_scale * 30), eff_glow);
    canvas->fillCircle(CX, CY, iris_r, C_SECONDARY);
    canvas->fillCircle(CX, CY, iris_r - 10, eff_accent);
    canvas->fillCircle(CX, CY, iris_r - 30, C_BG); 
    canvas->fillCircle(CX, CY, 15 + (int)(breath * 10) + (int)(audio_scale * 40), eff_high);

    // Spectrum Analyzer Rings
    int bin_count = 16;
    float start_r = 238.0f;
    float max_len = 90.0f;
    for (int i = 0; i < bin_count; i++) {
        float target = freq_bins[i] / 100.0f;
        if (target > visual_bins[i]) visual_bins[i] = target;
        else visual_bins[i] *= SPECTRUM_DECAY;
        float mag = visual_bins[i] * max_len;
        for (int mirror = 0; mirror < 2; mirror++) {
            float angle_deg = (i * (180.0f / bin_count)) + (mirror * 180.0f);
            float bar_width_deg = 4.0f; 
            for (float offset_deg = -bar_width_deg/2.0f; offset_deg <= bar_width_deg/2.0f; offset_deg += 0.5f) {
                float rad = (angle_deg + offset_deg) * (M_PI / 180.0f);
                int x0 = CX + (int)(cosf(rad) * start_r);
                int y0 = CY + (int)(sinf(rad) * start_r);
                int x1 = CX + (int)(cosf(rad) * (start_r - mag));
                int y1 = CY + (int)(sinf(rad) * (start_r - mag));
                if (abs(offset_deg) > (bar_width_deg/2.0f - 1.0f)) canvas->drawLine(x0, y0, x1, y1, C_SECONDARY);
                else canvas->drawLine(x0, y0, x1, y1, eff_accent);
            }
        }
    }
}

void AssistantView::_draw_face(float t) {
    uint32_t now = millis();

    // ── Handle Global Blinking ──────────────────────────────────────────────
    if (current_state == AssistantView::STATE_THINKING) {
        if (now >= next_blink_ms && !is_blinking) {
            is_blinking    = true;
            blink_end_ms   = now + 140; // Quick blink
            next_blink_ms  = now + 2500 + (rand() % 4500); // Random interval
        }
    } else {
        is_blinking = false; // Never blink outside thinking
    }
    
    if (is_blinking && now >= blink_end_ms) {
        is_blinking = false;
    }

    // ── Robot Face logic ────────────────────────────────────────────────────
    float eye_spacing = 85.0f;
    float eye_y       = CY - 45.0f;
    float base_eye_r  = 48.0f;
    float breath = (sinf(t * 1.5f) + 1.0f) * 0.5f;
    float eye_scale_v = 1.0f;
    uint16_t eye_col = C_HIGHLIGHT;
    uint16_t face_col = C_ACCENT;
    
    // Audio-reactive pulse (glow only, no narrowing)
    float pulse = audio_scale * 12.0f;

    if (current_state == AssistantView::STATE_LISTENING) {
        eye_scale_v = 1.0f; 
        eye_col = C_HIGHLIGHT; // Mint Green for heuristic scan
        face_col = C_ACCENT;
    } else if (current_state == AssistantView::STATE_THINKING) {
        eye_scale_v = 1.0f; // Don't narrow
        eye_col = 0x07FF; // Thinking Blue
    } else if (current_state == AssistantView::STATE_SPEAKING) {
        eye_scale_v = 1.0f;
        eye_col = C_ACCENT; // Gold
        face_col = C_ACCENT;
    } else if (current_state == AssistantView::STATE_CONTINUITY) {
        eye_scale_v = 1.1f; // Slightly wider "attentive" eyes
        eye_col = 0xFF40;   // Amber/Gold
        face_col = 0xFF40;
    }

    auto draw_eye = [&](float x, float y, float r_base, float sv, uint16_t col, bool is_wink = false) {
        if (is_wink || is_blinking) sv = 0.05f; 

        int r_h = (int)(r_base + pulse * 0.4f);
        int r_v = (int)(r_base * sv + pulse * 0.2f);
        if (r_v < 4) r_v = 4; // Absolute minimum visibility
        
        // Glow layer
        canvas->fillEllipse(x, y, r_h + 10, r_v + 8, C_GLOW);

        // Bold standard eye (Now same for Listening and Standby)
        canvas->fillEllipse(x, y, r_h, r_v, col);
        canvas->fillEllipse(x, y, (int)(r_h * 0.55f), (int)(r_v * 0.55f), C_BG); 

        if (sv > 0.40f) { // Focused detail
            canvas->fillEllipse(x, y, r_h * 0.20f, r_v * 0.20f, col);
        }

        // Emotion-specific shapes (Overlays)
        if (current_emotion == AssistantView::EMO_HAPPY && !is_wink && !is_blinking) {
            // Smile eyes
            for (int dx = -r_h; dx <= r_h; dx++) {
                float dy_top = -sqrtf(1.0f - powf(dx/(float)r_h, 2.0f)) * r_v;
                canvas->drawFastVLine(x + dx, y + dy_top, 10, col);
            }
        } else if (current_emotion == AssistantView::EMO_SARDONIC && !is_wink && !is_blinking) {
            // Sardonic slant
            for (int dy = -r_v; dy <= r_v; dy += 2) {
                float angle = asinf((float)abs(dy) / (float)r_v);
                int dx = (int)(cosf(angle) * r_h);
                int offset = (dy * 0.4f);
                canvas->drawFastHLine(x - dx + offset, y + dy, dx * 2, col);
            }
        }
    };

    bool wink_r = (current_emotion == AssistantView::EMO_WINK);
    draw_eye(CX - eye_spacing, eye_y, base_eye_r, eye_scale_v, eye_col);
    draw_eye(CX + eye_spacing, eye_y, base_eye_r, eye_scale_v, eye_col, wink_r);

    // Mouth logic
    float mouth_y = CY + 80.0f;
    float jaw_offset = (current_state == AssistantView::STATE_SPEAKING) ? (audio_scale * 12.0f) : 0.0f;
    mouth_y += jaw_offset;

    if (current_state == AssistantView::STATE_LISTENING || current_state == AssistantView::STATE_SPEAKING) {
        // Spectrum Analyzer Mouth
        int bin_w = 12;
        int spacing = 4;
        int total_w = (bin_w + spacing) * 12 - spacing;
        int start_x = CX - (total_w / 2);
        
        uint16_t bar_col = (current_state == AssistantView::STATE_LISTENING) ? C_HIGHLIGHT : C_ACCENT;

        for (int i = 0; i < 12; i++) {
            float mag = freq_bins[i + 2] * 0.6f; // Use mid-bins
            if (mag > visual_bins[i]) visual_bins[i] = mag;
            else visual_bins[i] *= 0.85f;
            
            int bh = (int)visual_bins[i] + 4;
            canvas->fillRect(start_x + i * (bin_w + spacing), mouth_y - (bh / 2), bin_w, bh, bar_col); 
        }
    } else {
        // Reactive Mouth Line (Neutral/Thinking/Idle)
        float mouth_w = 160.0f + (audio_scale * 20.0f);
        int prev_x = (int)(CX - (mouth_w/2.0f)), prev_y = (int)mouth_y;
        
        uint16_t mouth_col = face_col;

        for (int x = 0; x <= (int)mouth_w; x += 6) {
            float norm_x = (x / mouth_w) * 2.0f - 1.0f;
            float curve = 0.0f;
            if (current_emotion == AssistantView::EMO_HAPPY) curve = -22.0f * (1.0f - norm_x * norm_x);
            else if (current_emotion == AssistantView::EMO_SARDONIC) curve = 12.0f * norm_x;
            
            float phase = (t * 14.0f) + (x * 0.2f);
            float amp   = (4.0f + breath * 4.0f); // Steady breathing for idle
            
            int cur_x = (int)(CX - (mouth_w/2.0f)) + x;
            int cur_y = (int)(mouth_y + (sinf(phase) * amp) + curve);
            
            canvas->drawLine(prev_x, prev_y, cur_x, cur_y, mouth_col);
            canvas->drawLine(prev_x, prev_y + 1, cur_x, cur_y + 1, mouth_col); 
            canvas->drawLine(prev_x, prev_y + 2, cur_x, cur_y + 2, mouth_col); 
            prev_x = cur_x; prev_y = cur_y;
        }
    }

    // Modernized UI Status
    canvas->setTextSize(2);
    canvas->setTextColor(C_SECONDARY);
    canvas->setCursor(CX - 120, CY + 175);
    const char* ms = "MODE: STANDBY";
    if (current_state == AssistantView::STATE_LISTENING) ms = "MODE: HEURISTIC_SCAN";
    else if (current_state == AssistantView::STATE_THINKING) ms = "MODE: PROCESSING...";
    else if (current_state == AssistantView::STATE_SPEAKING) ms = "MODE: SYNTH_OUT";
    else if (current_state == AssistantView::STATE_CONTINUITY) ms = "MODE: CONTINUITY";
    canvas->print(ms);

    if (current_emotion != AssistantView::EMO_NEUTRAL) {
        canvas->setCursor(CX - 120, CY + 195);
        const char* es = "";
        switch(current_emotion) {
            case AssistantView::EMO_HAPPY:    es = "BIAS: POSITIVE"; break;
            case AssistantView::EMO_SARDONIC: es = "BIAS: SARDONIC"; break;
            case AssistantView::EMO_ALERT:    es = "BIAS: ALERT_PRIORITY"; break;
            case AssistantView::EMO_WINK:     es = "BIAS: GESTURE_ACK"; break;
            default: break;
        }
        canvas->print(es);
    }
}

void AssistantView::update() {
    if (!canvas) return;
    unsigned long now = millis();
    float t = (now - start_ms) / 1000.0f;
    canvas->fillScreen(C_BG);

    float target_audio = cur_audio_intensity / 100.0f;
    if (target_audio > audio_scale) audio_scale = target_audio;
    else audio_scale *= AUDIO_DECAY;

    // Dispatch based on style
    if (current_style == STYLE_FACE) {
        _draw_face(t);
    } else {
        _draw_iris(t);
    }

    // 4. Timer ring — lerp toward true value for smooth assistant-view animation
    static float draw_pct = 100.0f;
    if (timer_active && timer_pct_vis >= 0.0f) {
        draw_pct += (timer_pct_vis - draw_pct) * 0.12f;  // smooth at ~20fps draw rate
        float span_deg  = (draw_pct / 100.0f) * 360.0f;
        float start_rad = -M_PI / 2.0f;
        float end_rad   = start_rad + span_deg * (M_PI / 180.0f);
        uint16_t col    = timer_ring_color((int)draw_pct);
        int ring_r      = 240;
        int thickness   = 10;
        for (float a = start_rad; a <= end_rad; a += 0.008f) {
            float cs = cosf(a), sn = sinf(a);
            for (int tt = 0; tt < thickness; tt++) {
                canvas->drawPixel(CX + (int)((ring_r - tt) * cs),
                                  CY + (int)((ring_r - tt) * sn), col);
            }
        }
    }

    canvas->flush();
}
