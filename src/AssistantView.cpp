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

static Arduino_Canvas *canvas = nullptr;
static unsigned long start_ms = 0;
static int cur_audio_intensity = 0;
static float audio_scale = 0.0f; // Smoothed visual scale
int AssistantView::freq_bins[16] = {0};
static float visual_bins[16] = {0.0f}; // Smoothed visual values

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
        audio_scale *= 0.85f; // Gradual decay
    }

    // 1. Draw Breathing Iris (Central Orb)
    float base_breath = (sinf(t * 2.0f) + 1.0f) * 0.5f; 
    float breath = base_breath + (audio_scale * 3.0f); // Boosted from 1.5f
    int iris_r = 60 + (int)(breath * 25); // Increased from 20
    
    // Multi-layered glow
    canvas->fillCircle(CX, CY, iris_r + 15 + (int)(audio_scale * 30), C_GLOW);
    canvas->fillCircle(CX, CY, iris_r, C_SECONDARY);
    canvas->fillCircle(CX, CY, iris_r - 10, C_ACCENT);

    canvas->fillCircle(CX, CY, iris_r - 30, C_BG); // Core
    
    // Core detail (pupil)
    canvas->fillCircle(CX, CY, 15 + (int)(breath * 10) + (int)(audio_scale * 40), C_HIGHLIGHT);


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
        else visual_bins[i] *= 0.88f; // Smooth decay

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
                    canvas->drawLine(x0, y0, x1, y1, C_ACCENT);
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

    canvas->flush();
}
