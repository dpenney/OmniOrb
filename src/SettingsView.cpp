#include "SettingsView.h"
#include <math.h>

// ─── OmniOrb palette ─────────────────────────────────────────────────────────
static const uint16_t SV_BG    = 0x0000;  // Black
static const uint16_t SV_BRASS = 0xE651;  // Brass Gold  (matches AssistantView C_ACCENT)
static const uint16_t SV_MINT  = 0x97F2;  // Mint Green  (matches AssistantView C_HIGHLIGHT)
static const uint16_t SV_DIM   = 0x4228;  // Dark warm grey — inactive arc track
static const uint16_t SV_HINT  = 0x2104;  // Very dim — hint text
static const uint16_t SV_WHITE = 0xFFFF;  // White — large value readout

static const int CX = 240;
static const int CY = 240;

// ─── Knob geometry ───────────────────────────────────────────────────────────
// Standard screen coords: 0° = right (3-o'clock), positive = clockwise
//   135° = SW (7-o'clock)  → arc start (vol = 0%)
//   270° = top (12-o'clock) → vol = 50%
//   405° = SE (5-o'clock)  → arc end  (vol = 100%)
// 270° total sweep, gap at the bottom (90° = 6-o'clock)
static const float ARC_START  = 135.0f;  // degrees
static const float ARC_SPAN   = 270.0f;  // total sweep
static const int   ARC_R_OUT  = 228;     // outer edge of arc track
static const int   ARC_R_IN   = 212;     // inner edge (16 px thick)
static const int   TICK_R_OUT = 232;     // ticks extend to bezel (gets clipped)
static const int   TICK_R_IN  = 207;     // tick base (5 px inside inner edge)
static const int   DECO_R1    = 190;     // inner decorative ring
static const int   DECO_R2    = 135;     // second decorative ring

static Arduino_Canvas    *sv_canvas    = nullptr;
static SemaphoreHandle_t  sv_vsync_sem = NULL;
static int sv_vol = 75;

static inline float vol_to_deg(int v) {
    return ARC_START + (v / 100.0f) * ARC_SPAN;
}

void SettingsView::set_canvas(Arduino_Canvas *c)    { sv_canvas    = c; }
void SettingsView::set_vsync_sem(SemaphoreHandle_t s) { sv_vsync_sem = s; }
void SettingsView::init(int vol)                 { sv_vol = constrain(vol, 0, 100); }
void SettingsView::show()                        {}
void SettingsView::hide()                        {}
void SettingsView::set_volume(int v)             { sv_vol = constrain(v, 0, 100); }
void SettingsView::adjust_volume(int d)          { sv_vol = constrain(sv_vol + d, 0, 100); }
int  SettingsView::get_volume()                  { return sv_vol; }

int SettingsView::touch_to_volume(int tx, int ty) {
    float angle_deg = atan2f((float)(ty - CY), (float)(tx - CX)) * (180.0f / M_PI);
    if (angle_deg < 0) angle_deg += 360.0f;
    // Normalize to arc-relative angle: 0° = vol 0%, ARC_SPAN° = vol 100%
    float rel = angle_deg - ARC_START;
    if (rel < 0) rel += 360.0f;
    if (rel > ARC_SPAN) {
        // In the gap (bottom dead zone) — clamp to nearest end
        float dist_end   = rel - ARC_SPAN;
        float dist_start = 360.0f - rel;
        rel = (dist_end < dist_start) ? ARC_SPAN : 0.0f;
    }
    return constrain((int)roundf((rel / ARC_SPAN) * 100.0f), 0, 100);
}

void SettingsView::update() {
    if (!sv_canvas) return;
    sv_canvas->fillScreen(SV_BG);

    // ── 1. Arc track — single pass: dim for inactive, brass for active ────────
    float lit_end = vol_to_deg(sv_vol);
    float arc_end = ARC_START + ARC_SPAN;
    for (float a = ARC_START; a <= arc_end + 0.1f; a += 0.4f) {
        float rad = a * (M_PI / 180.0f);
        float cs = cosf(rad), sn = sinf(rad);
        uint16_t col = (a <= lit_end) ? SV_BRASS : SV_DIM;
        sv_canvas->drawLine(
            CX + (int)(cs * ARC_R_IN),  CY + (int)(sn * ARC_R_IN),
            CX + (int)(cs * ARC_R_OUT), CY + (int)(sn * ARC_R_OUT),
            col);
    }

    // ── 2. Tick marks at every 10% (11 marks) ─────────────────────────────────
    for (int i = 0; i <= 10; i++) {
        float pct = i * 10.0f;
        float deg = vol_to_deg((int)pct);
        float rad = deg * (M_PI / 180.0f);
        float cs = cosf(rad), sn = sinf(rad);
        // Major ticks (0, 50, 100) extend a bit deeper
        int r_in = (i % 5 == 0) ? TICK_R_IN - 4 : TICK_R_IN;
        uint16_t tcol = (pct <= (float)sv_vol) ? SV_BRASS : SV_DIM;
        sv_canvas->drawLine(
            CX + (int)(cs * r_in),       CY + (int)(sn * r_in),
            CX + (int)(cs * TICK_R_OUT), CY + (int)(sn * TICK_R_OUT),
            tcol);
    }

    // ── 3. Indicator dot at current position ──────────────────────────────────
    float ind_rad = lit_end * (M_PI / 180.0f);
    int dot_r = (ARC_R_IN + ARC_R_OUT) / 2;
    int dot_x = CX + (int)(cosf(ind_rad) * dot_r);
    int dot_y = CY + (int)(sinf(ind_rad) * dot_r);
    sv_canvas->fillCircle(dot_x, dot_y, 6, SV_WHITE);
    sv_canvas->fillCircle(dot_x, dot_y, 3, SV_MINT);

    // ── 4. Inner decorative rings ─────────────────────────────────────────────
    sv_canvas->drawCircle(CX, CY, DECO_R1,     SV_DIM);
    sv_canvas->drawCircle(CX, CY, DECO_R1 - 1, SV_DIM);
    sv_canvas->drawCircle(CX, CY, DECO_R2,     SV_HINT);

    // ── 5. Header — "NEXUS // SETTINGS" ──────────────────────────────────────
    sv_canvas->setTextColor(SV_MINT);
    sv_canvas->setTextSize(1);
    // "OMNIORB SETTINGS" = 16 chars × 6px = 96px wide
    sv_canvas->setCursor(CX - 48, 68);
    sv_canvas->print("OMNIORB SETTINGS");
    sv_canvas->drawFastHLine(CX - 80, 80, 160, SV_DIM);

    // ── 6. "VOLUME" setting label ─────────────────────────────────────────────
    sv_canvas->setTextColor(SV_BRASS);
    sv_canvas->setTextSize(2);
    // "VOLUME" = 6 chars × 12px = 72px wide
    sv_canvas->setCursor(CX - 36, 158);
    sv_canvas->print("VOLUME");

    // ── 7. Large value number (size 6 = 36px/char, 48px tall) ────────────────
    char vstr[8];
    snprintf(vstr, sizeof(vstr), "%d", sv_vol);
    int nchars = strlen(vstr);
    int char_w = 36;  // 6px × textSize(6)
    sv_canvas->setTextColor(SV_WHITE);
    sv_canvas->setTextSize(6);
    sv_canvas->setCursor(CX - (nchars * char_w) / 2, 192);
    sv_canvas->print(vstr);

    // Percent superscript
    sv_canvas->setTextColor(SV_MINT);
    sv_canvas->setTextSize(2);
    sv_canvas->setCursor(CX + (nchars * char_w) / 2 + 4, 200);
    sv_canvas->print("%");

    // ── 8. Diagnostics Button ────────────────────────────────────────────────
    sv_canvas->drawRoundRect(CX - 100, 275, 200, 45, 6, SV_DIM);
    sv_canvas->setTextColor(SV_MINT);
    sv_canvas->setTextSize(1);
    sv_canvas->setCursor(CX - 54, 292);
    sv_canvas->print("AUDIO DIAGNOSTICS");

    // ── 9. Hint text ─────────────────────────────────────────────────────────
    sv_canvas->setTextColor(SV_HINT);
    sv_canvas->setTextSize(1);
    // "SWIPE RIGHT TO EXIT" = 18 chars × 6px = 108px
    sv_canvas->setCursor(CX - 54, 395);
    sv_canvas->print("SWIPE RIGHT TO EXIT");

    // Sync to VSYNC before flushing — prevents DMA writing mid-scan (tearing/blink)
    if (sv_vsync_sem) xSemaphoreTake(sv_vsync_sem, portMAX_DELAY);
    sv_canvas->flush();
}
