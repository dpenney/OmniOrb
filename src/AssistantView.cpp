#include "AssistantView.h"
#include <math.h>

// ─── Constants ─────────────────────────────────────────────────────────────
static const uint16_t C_BG        = 0x0000;
static const uint16_t C_HAL_RED      = 0xF800; // #FF0000
static const uint16_t C_HAL_ORANGE   = 0xFEA0; // #FFD700ish
static const uint16_t C_HAL_DARK_RED = 0x8000;
static const uint16_t C_HAL_GLOW     = 0x4000;

static const int CX = 240;
static const int CY = 240;
static const int SCREEN_RADIUS = 230;

static Arduino_Canvas *canvas = nullptr;
static unsigned long start_ms = 0;
static int cur_audio_intensity = 0;
static float audio_scale = 0.0f; // Smoothed visual scale

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
        Serial.printf("Got Intensity: %d\n", intensity);
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
    canvas->fillCircle(CX, CY, iris_r + 15 + (int)(audio_scale * 30), C_HAL_GLOW);
    canvas->fillCircle(CX, CY, iris_r, C_HAL_DARK_RED);
    canvas->fillCircle(CX, CY, iris_r - 10, C_HAL_RED);

    canvas->fillCircle(CX, CY, iris_r - 30, C_BG); // Core
    
    // Core detail (pupil)
    canvas->fillCircle(CX, CY, 15 + (int)(breath * 10) + (int)(audio_scale * 40), C_HAL_ORANGE);


    // 2. Rotating Data Rings
    float rot1 = t * 0.4f; // Slower clockwise
    float rot2 = -t * 0.6f; // Slower counter-clockwise


    // Ring 1: Inner Dashed
    int r1 = 120;
    for (int a = 0; a < 360; a += 10) {
        float rad = (a + rot1 * 57.29f) * (M_PI / 180.0f);
        int sx = CX + (int)(cosf(rad) * r1);
        int sy = CY + (int)(sinf(rad) * r1);
        canvas->drawPixel(sx, sy, C_HAL_RED);

    }
    canvas->drawCircle(CX, CY, r1, C_HAL_DARK_RED);


    // Ring 2: Outer Hex-like markers
    int r2 = 180;
    for (int a = 0; a < 360; a += 30) {
        float rad = (a + rot2 * 57.29f) * (M_PI / 180.0f);
        int x0 = CX + (int)(cosf(rad) * (r2 - 10));
        int y0 = CY + (int)(sinf(rad) * (r2 - 10));
        int x1 = CX + (int)(cosf(rad) * (r2 + 10));
        int y1 = CY + (int)(sinf(rad) * (r2 + 10));
        canvas->drawLine(x0, y0, x1, y1, C_HAL_RED);

    }
    canvas->drawCircle(CX, CY, r2, C_HAL_DARK_RED);


    // 3. Scanline Animation
    int scan_y = (int)(CX + sinf(t * 1.5f) * SCREEN_RADIUS);
    if (scan_y > CY - SCREEN_RADIUS && scan_y < CY + SCREEN_RADIUS) {
        // Find x-width at this y
        int dy = abs(scan_y - CY);
        int dx = (int)sqrtf(SCREEN_RADIUS * SCREEN_RADIUS - dy * dy);
        canvas->drawFastHLine(CX - dx, scan_y, dx * 2, C_HAL_GLOW);

    }

    // 4. Floating Bits (Random-ish looking moving pixels)
    for (int i = 0; i < 5; i++) {
        float bt = t + i * 1.23f;
        int bx = CX + (int)(cosf(bt * 0.7f) * 200);
        int by = CY + (int)(sinf(bt * 1.1f) * 200);
        if ((bx-CX)*(bx-CX) + (by-CY)*(by-CY) < SCREEN_RADIUS*SCREEN_RADIUS) {
            canvas->drawPixel(bx, by, C_HAL_RED);

        }
    }

    canvas->flush();
}
