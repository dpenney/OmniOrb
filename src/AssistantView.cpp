#include "AssistantView.h"
#include <math.h>

// ─── Constants ─────────────────────────────────────────────────────────────
static const uint16_t C_BG        = 0x2A26; // Dark Green/Shadow
static const uint16_t C_ACCENT    = 0xE651; // Brass Gold
static const uint16_t C_HIGHLIGHT = 0x97F2; // Mint Green
static const uint16_t C_SECONDARY = 0x5AC9; // Secondary Neutral
static const uint16_t C_GLOW      = 0x5AC9; // Using Secondary for Glow

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
static AssistantView::AssistantState current_state = AssistantView::STATE_IDLE;

// ─── Timer state ────────────────────────────────────────────────────────────
static bool     timer_active   = false;
static uint32_t timer_start_ms = 0;
static uint32_t timer_dur_ms   = 0;
static String   timer_lbl      = "";
static bool     timer_done_flag = false;  // true for one update() frame after expiry
static float    timer_pct_vis  = 100.0f;  // smoothed visual percent

void AssistantView::set_state(AssistantState state) {
    current_state = state;
}

AssistantView::AssistantState AssistantView::get_state() {
    return current_state;
}

void AssistantView::set_canvas(Arduino_Canvas *c) {
    canvas = c;
}

void AssistantView::init() {
    start_ms = millis();
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

void AssistantView::update() {
    if (!canvas) return;

    unsigned long now = millis();
    float t = (now - start_ms) / 1000.0f;

    canvas->fillScreen(C_BG);

    // Use audio intensity to boost the visuals
    // Apply some smoothing/decay
    float target_audio = cur_audio_intensity / 100.0f; // Assume 0-100 scale
    if (target_audio > audio_scale) {
        audio_scale = target_audio; // Sharp rise
    } else {
        audio_scale *= AUDIO_DECAY;
    }

    // 1. Draw Breathing Iris (Central Orb)
    float base_breath = (sinf(t * 2.0f) + 1.0f) * 0.5f; 
    float breath = base_breath + (audio_scale * 3.0f); // Boosted from 1.5f
    int iris_r = 60 + (int)(breath * 25); // Increased from 20
    
    // Multi-layered glow
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
    }

    canvas->fillCircle(CX, CY, iris_r + 15 + (int)(audio_scale * 30), eff_glow);
    canvas->fillCircle(CX, CY, iris_r, C_SECONDARY);
    canvas->fillCircle(CX, CY, iris_r - 10, eff_accent);

    canvas->fillCircle(CX, CY, iris_r - 30, C_BG); // Core
    
    // Core detail (pupil)
    canvas->fillCircle(CX, CY, 15 + (int)(breath * 10) + (int)(audio_scale * 40), eff_high);


    // // 2. Rotating Data Rings
    // float rot1 = t * 0.4f; // Slower clockwise
    // float rot2 = -t * 0.6f; // Slower counter-clockwise


    // // Ring 1: Inner Dashed
    // int r1 = 120;
    // for (int a = 0; a < 360; a += 10) {
    //     float rad = (a + rot1 * 57.29f) * (M_PI / 180.0f);
    //     int sx = CX + (int)(cosf(rad) * r1);
    //     int sy = CY + (int)(sinf(rad) * r1);
    //     canvas->drawPixel(sx, sy, C_ACCENT);

    // }
    // canvas->drawCircle(CX, CY, r1, C_SECONDARY);


    // // Ring 2: Outer Hex-like markers
    // int r2 = 180;
    // for (int a = 0; a < 360; a += 30) {
    //     float rad = (a + rot2 * 57.29f) * (M_PI / 180.0f);
    //     int x0 = CX + (int)(cosf(rad) * (r2 - 10));
    //     int y0 = CY + (int)(sinf(rad) * (r2 - 10));
    //     int x1 = CX + (int)(cosf(rad) * (r2 + 10));
    //     int y1 = CY + (int)(sinf(rad) * (r2 + 10));
    //     canvas->drawLine(x0, y0, x1, y1, C_ACCENT);

    // }
    // canvas->drawCircle(CX, CY, r2, C_SECONDARY);


    // 3. Spectrum Analyzer Outer Ring (Growing Inwards)
    int bin_count = 16;
    float start_r = 238.0f;
    float max_len = 90.0f; // Increased from 40 for much taller bars

    for (int i = 0; i < bin_count; i++) {
        // Smoothing for each bin
        float target = freq_bins[i] / 100.0f;
        if (target > visual_bins[i]) visual_bins[i] = target;
        else visual_bins[i] *= SPECTRUM_DECAY;

        float mag = visual_bins[i] * max_len;
        
        // Render 2 bars per frequency bin for symmetry (mirrored)
        for (int mirror = 0; mirror < 2; mirror++) {
            float angle_deg = (i * (180.0f / bin_count)) + (mirror * 180.0f);
            
            // Draw a wider bar by drawing multiple lines (e.g. 4 degrees wide)
            float bar_width_deg = 4.0f; 
            for (float offset_deg = -bar_width_deg/2.0f; offset_deg <= bar_width_deg/2.0f; offset_deg += 0.5f) {
                float rad = (angle_deg + offset_deg) * (M_PI / 180.0f);

                int x0 = CX + (int)(cosf(rad) * start_r);
                int y0 = CY + (int)(sinf(rad) * start_r);
                int x1 = CX + (int)(cosf(rad) * (start_r - mag));
                int y1 = CY + (int)(sinf(rad) * (start_r - mag));

                // If it's near the edge of the bar, draw it 3D/glow effect
                if (abs(offset_deg) > (bar_width_deg/2.0f - 1.0f)) {
                    canvas->drawLine(x0, y0, x1, y1, C_SECONDARY);
                } else {
                    canvas->drawLine(x0, y0, x1, y1, eff_accent);
                }
            }
        }
    }


    // // 3. Scanline Animation
    // int scan_y = (int)(CX + sinf(t * 1.5f) * SCREEN_RADIUS);
    // if (scan_y > CY - SCREEN_RADIUS && scan_y < CY + SCREEN_RADIUS) {
    //     // Find x-width at this y
    //     int dy = abs(scan_y - CY);
    //     int dx = (int)sqrtf(SCREEN_RADIUS * SCREEN_RADIUS - dy * dy);
    //     canvas->drawFastHLine(CX - dx, scan_y, dx * 2, C_GLOW);

    // }

    // // 4. Floating Bits (Random-ish looking moving pixels)
    // for (int i = 0; i < 5; i++) {
    //     float bt = t + i * 1.23f;
    //     int bx = CX + (int)(cosf(bt * 0.7f) * 200);
    //     int by = CY + (int)(sinf(bt * 1.1f) * 200);
    //     if ((bx-CX)*(bx-CX) + (by-CY)*(by-CY) < SCREEN_RADIUS*SCREEN_RADIUS) {
    //         canvas->drawPixel(bx, by, C_ACCENT);

    //     }
    // }

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
