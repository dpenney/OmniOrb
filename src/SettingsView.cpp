#include "SettingsView.h"
#include <math.h>
#include <WiFi.h>

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
static Arduino_GFX       *sv_gfx      = nullptr;  // direct panel target for incremental draws
static SemaphoreHandle_t  sv_vsync_sem = NULL;
static int  sv_vol            = 75;
static int  sv_prev_vol       = -1;    // last vol drawn incrementally; -1 = unknown
static bool sv_needs_full_draw = true;  // force full canvas flush on screen entry
static int  sv_pi_rssi        = 0;

static inline float vol_to_deg(int v) {
    return ARC_START + (v / 100.0f) * ARC_SPAN;
}

void SettingsView::set_canvas(Arduino_Canvas *c) { sv_canvas    = c; }
void SettingsView::set_gfx(Arduino_GFX *g)       { sv_gfx      = g; }
void SettingsView::set_vsync_sem(SemaphoreHandle_t s) { sv_vsync_sem = s; }
void SettingsView::init(int vol)    { sv_vol = constrain(vol, 0, 100); }
void SettingsView::show()           { sv_needs_full_draw = true; sv_prev_vol = -1; }
void SettingsView::hide()           {}
void SettingsView::set_volume(int v)    { sv_vol = constrain(v, 0, 100); }
void SettingsView::adjust_volume(int d) { sv_vol = constrain(sv_vol + d, 0, 100); }
int  SettingsView::get_volume()         { return sv_vol; }
void SettingsView::set_pi_rssi(int rssi) { 
    if (sv_pi_rssi != rssi) {
        sv_pi_rssi = rssi; 
        if (sv_gfx && !sv_needs_full_draw) {
            sv_gfx->fillRect(CX + 15, 120, 100, 10, SV_BG);
            sv_gfx->setTextColor(SV_WHITE);
            sv_gfx->setTextSize(1);
            sv_gfx->setCursor(CX + 15, 120);
            if (sv_pi_rssi != 0) sv_gfx->printf("%d dBm", sv_pi_rssi);
            else sv_gfx->print("OFFLINE");
        }
    }
}

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

// Draw the elements that change on every arc drag: arc track, tick marks,
// indicator dot, and volume number. Works with any Arduino_GFX target so it
// can be called against the canvas (full-draw path) or directly against gfx
// (incremental path, no canvas→framebuffer copy required).
// prev_vol: the volume drawn on the previous call (-1 = unknown / first draw).
// When known, the old indicator dot is erased before the arc is redrawn.
static void sv_draw_dynamic(Arduino_GFX *tgt, int vol, int prev_vol = -1) {
    float lit_end = vol_to_deg(vol);
    float arc_end = ARC_START + ARC_SPAN;

    // Erase the old indicator dot before redrawing the arc. The arc lines alone
    // at 0.2° density can still leave corner pixels of the 6px dot uncovered.
    if (prev_vol >= 0 && prev_vol != vol) {
        float old_rad = vol_to_deg(prev_vol) * (M_PI / 180.0f);
        int d_r = (ARC_R_IN + ARC_R_OUT) / 2;
        tgt->fillCircle(CX + (int)(cosf(old_rad) * d_r),
                        CY + (int)(sinf(old_rad) * d_r), 7, SV_BG);
    }

    // Arc track — 0.2° step ensures gap-free pixel coverage at the outer radius
    // (228px × 0.2° × π/180 ≈ 0.8px between lines, < 1px so no pixel is skipped).
    for (float a = ARC_START; a <= arc_end + 0.1f; a += 0.2f) {
        float rad = a * (M_PI / 180.0f);
        float cs = cosf(rad), sn = sinf(rad);
        tgt->drawLine(CX + (int)(cs * ARC_R_IN),  CY + (int)(sn * ARC_R_IN),
                      CX + (int)(cs * ARC_R_OUT), CY + (int)(sn * ARC_R_OUT),
                      (a <= lit_end) ? SV_BRASS : SV_DIM);
    }

    // Tick marks at every 10% (11 marks)
    for (int i = 0; i <= 10; i++) {
        float pct = i * 10.0f;
        float rad = vol_to_deg((int)pct) * (M_PI / 180.0f);
        float cs = cosf(rad), sn = sinf(rad);
        int r_in = (i % 5 == 0) ? TICK_R_IN - 4 : TICK_R_IN;
        tgt->drawLine(CX + (int)(cs * r_in),       CY + (int)(sn * r_in),
                      CX + (int)(cs * TICK_R_OUT), CY + (int)(sn * TICK_R_OUT),
                      (pct <= (float)vol) ? SV_BRASS : SV_DIM);
    }

    // Indicator dot — drawn after the arc so it sits on top
    float ind_rad = lit_end * (M_PI / 180.0f);
    int dot_r = (ARC_R_IN + ARC_R_OUT) / 2;
    int dot_x = CX + (int)(cosf(ind_rad) * dot_r);
    int dot_y = CY + (int)(sinf(ind_rad) * dot_r);
    tgt->fillCircle(dot_x, dot_y, 6, SV_WHITE);
    tgt->fillCircle(dot_x, dot_y, 3, SV_MINT);

    // Volume number — erase the full worst-case area (3 digits + %) then redraw.
    // Rect covers x=186..310, y=192..240 — the widest possible "100%" at textSize(6).
    tgt->fillRect(186, 192, 124, 48, SV_BG);
    char vstr[8];
    snprintf(vstr, sizeof(vstr), "%d", vol);
    int nchars = strlen(vstr);
    int char_w = 36;  // 6px × textSize(6)
    tgt->setTextColor(SV_WHITE);
    tgt->setTextSize(6);
    tgt->setCursor(CX - (nchars * char_w) / 2, 192);
    tgt->print(vstr);
    tgt->setTextColor(SV_MINT);
    tgt->setTextSize(2);
    tgt->setCursor(CX + (nchars * char_w) / 2 + 4, 200);
    tgt->print("%");
}

void SettingsView::update() {
    if (!sv_canvas) return;

    if (sv_needs_full_draw || !sv_gfx) {
        // Full path: render everything to the off-screen canvas, then flush once to
        // the panel framebuffer. This runs only on screen entry — not during arc drag.
        sv_canvas->fillScreen(SV_BG);
        sv_draw_dynamic(sv_canvas, sv_vol);

        // Static elements (unchanged between arc drags)
        sv_canvas->drawCircle(CX, CY, DECO_R1,     SV_DIM);
        sv_canvas->drawCircle(CX, CY, DECO_R1 - 1, SV_DIM);
        sv_canvas->drawCircle(CX, CY, DECO_R2,     SV_HINT);

        sv_canvas->setTextColor(SV_MINT);
        sv_canvas->setTextSize(1);
        sv_canvas->setCursor(CX - 48, 68);
        sv_canvas->print("OMNIORB SETTINGS");
        sv_canvas->drawFastHLine(CX - 80, 80, 160, SV_DIM);

        sv_canvas->setTextColor(SV_MINT);
        sv_canvas->setTextSize(1);
        sv_canvas->setCursor(CX - 60, 100);
        sv_canvas->print("RADAR WIFI:");
        sv_canvas->setCursor(CX - 60, 120);
        sv_canvas->print("BRAIN WIFI:");
        
        sv_canvas->setTextColor(SV_WHITE);
        int my_rssi = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 0;
        sv_canvas->setCursor(CX + 15, 100);
        if (my_rssi != 0) sv_canvas->printf("%d dBm", my_rssi);
        else sv_canvas->print("OFFLINE");

        sv_canvas->setCursor(CX + 15, 120);
        if (sv_pi_rssi != 0) sv_canvas->printf("%d dBm", sv_pi_rssi);
        else sv_canvas->print("OFFLINE");

        sv_canvas->setTextColor(SV_BRASS);
        sv_canvas->setTextSize(2);
        sv_canvas->setCursor(CX - 36, 158);
        sv_canvas->print("VOLUME");

        sv_canvas->drawRoundRect(CX - 100, 275, 200, 45, 6, SV_DIM);
        sv_canvas->setTextColor(SV_MINT);
        sv_canvas->setTextSize(1);
        sv_canvas->setCursor(CX - 54, 292);
        sv_canvas->print("AUDIO DIAGNOSTICS");

        sv_canvas->setTextColor(SV_HINT);
        sv_canvas->setTextSize(1);
        sv_canvas->setCursor(CX - 54, 395);
        sv_canvas->print("SWIPE RIGHT TO EXIT");

        // Drain any stale VSYNC token then wait for a genuine blanking edge before
        // flushing. The 460KB canvas→framebuffer copy still takes ~12ms (longer than
        // the 1.5ms blanking window), but starting at the edge minimises mid-scan tearing.
        if (sv_vsync_sem) {
            xSemaphoreTake(sv_vsync_sem, 0);
            xSemaphoreTake(sv_vsync_sem, portMAX_DELAY);
        }
        sv_canvas->flush();
        sv_needs_full_draw = false;
        sv_prev_vol = sv_vol;  // canvas is now the ground truth at sv_vol
    } else {
        // Incremental path: update only the arc+number region directly on the
        // panel framebuffer — no 460KB canvas copy, no PSRAM bus monopolisation.
        // _auto_flush on gfx handles Cache_WriteBack per draw call (tiny regions).
        sv_draw_dynamic(sv_gfx, sv_vol, sv_prev_vol);
        sv_prev_vol = sv_vol;
    }
}
