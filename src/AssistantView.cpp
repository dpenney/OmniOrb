#include "AssistantView.h"
#include <math.h>

extern bool pi_connected;

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

static const float AUDIO_DECAY       = 0.85f;
static const float SPECTRUM_DECAY    = 0.88f;

static Arduino_Canvas    *canvas       = nullptr;
static Arduino_GFX       *output_gfx   = nullptr;
static Arduino_GFX       *av_gfx      = nullptr; // points to canvas when active
static SemaphoreHandle_t  av_vsync_sem = NULL;
static unsigned long start_ms = 0;
static int cur_audio_intensity = 0;
static float audio_scale = 0.0f;
int AssistantView::freq_bins[16] = {0};
static float visual_bins[16] = {0.0f};

// Iris incremental erase: tracks glow radius from last frame so we only erase
// the shrinking annulus instead of the full 460KB fillScreen.
static int av_prev_glow_r = -1;

// Face state tracking
static int   face_prev_l_rh = 0, face_prev_l_rv = 0;
static int   face_prev_r_rh = 0, face_prev_r_rv = 0;
static float face_prev_bar_h[12] = {};
static float face_prev_mouth_y   = 0.0f;
static bool  face_prev_was_bars  = false;
static AssistantView::AssistantState face_text_state   = AssistantView::STATE_IDLE;
static AssistantView::Emotion        face_text_emotion = AssistantView::EMO_NEUTRAL;
// Mouth line path tracking: erase per-segment instead of fillRect
static int   mouth_prev_px[34] = {};
static int   mouth_prev_py[34] = {};
static int   mouth_prev_n      = 0;

// ─── Timer state ────────────────────────────────────────────────────────────
static bool     timer_active   = false;
static uint32_t timer_start_ms = 0;
static uint32_t timer_dur_ms   = 0;
static String   timer_lbl      = "";
static bool     timer_done_flag = false;
static float    timer_pct_vis  = 100.0f;

static AssistantView::AssistantState current_state   = AssistantView::STATE_IDLE;
static AssistantView::Emotion       current_emotion  = AssistantView::EMO_NEUTRAL;
static AssistantView::AssistantStyle current_style   = AssistantView::STYLE_FACE;

// ─── Expressive state ──────────────────────────────────────────────────────
static uint32_t next_blink_ms = 0;
static uint32_t blink_end_ms  = 0;
static bool     is_blinking   = false;

void AssistantView::set_state(AssistantState state) {
    if (state != current_state) {
        cur_audio_intensity = 0;
        audio_scale = 0.0f;
        for (int i = 0; i < 16; i++) {
            freq_bins[i] = 0;
            visual_bins[i] = 0.0f;
        }
        current_state = state;
    }
}

AssistantView::AssistantState AssistantView::get_state() { return current_state; }
void AssistantView::set_emotion(Emotion e)               { current_emotion = e; }
AssistantView::Emotion AssistantView::get_emotion()      { return current_emotion; }
void AssistantView::set_style(AssistantStyle s)          { current_style = s; }
AssistantView::AssistantStyle AssistantView::get_style() { return current_style; }

void AssistantView::toggle_style() {
    current_style = (current_style == STYLE_IRIS) ? STYLE_FACE : STYLE_IRIS;
    // Reset incremental state for both styles so the new style starts clean
    av_prev_glow_r = -1;
    memset(face_prev_bar_h, 0, sizeof(face_prev_bar_h));
    face_prev_mouth_y   = 0.0f;
    face_prev_was_bars  = false;
    face_prev_l_rh = 0; face_prev_l_rv = 0;
    face_prev_r_rh = 0; face_prev_r_rv = 0;
    face_text_state   = (AssistantView::AssistantState)-1;
    face_text_emotion = (AssistantView::Emotion)-1;
    mouth_prev_n = 0;
    if (av_gfx) {
        if (av_vsync_sem) { xSemaphoreTake(av_vsync_sem, 0); xSemaphoreTake(av_vsync_sem, pdMS_TO_TICKS(50)); }
        av_gfx->fillScreen(C_BG);
    }
}

void AssistantView::set_canvas(Arduino_Canvas *c) { 
    canvas = c; 
    av_gfx = c; // All drawing now goes to the buffer!
}

void AssistantView::set_gfx(Arduino_GFX *gfx) { output_gfx = gfx; }

void AssistantView::set_vsync_sem(SemaphoreHandle_t sem) { av_vsync_sem = sem; }

void AssistantView::init() {
    start_ms = millis();
    next_blink_ms = start_ms + 2000;
}

void AssistantView::show() {
    start_ms = millis();
    av_prev_glow_r = -1;
    memset(face_prev_bar_h, 0, sizeof(face_prev_bar_h));
    face_prev_mouth_y   = 0.0f;
    face_prev_was_bars  = false;
    face_prev_l_rh = 0; face_prev_l_rv = 0;
    face_prev_r_rh = 0; face_prev_r_rv = 0;
    face_text_state   = (AssistantView::AssistantState)-1;
    face_text_emotion = (AssistantView::Emotion)-1;
    mouth_prev_n = 0;
    for (int i = 0; i < 16; i++) visual_bins[i] = 0.0f;
    if (!av_gfx) return;
    // VSYNC-sync the one-time full clear to minimise tearing on entry
    if (av_vsync_sem) {
        xSemaphoreTake(av_vsync_sem, 0);
        xSemaphoreTake(av_vsync_sem, pdMS_TO_TICKS(50));
    }
    av_gfx->fillScreen(C_BG);
    if (canvas && output_gfx) canvas->flush(); // One-time FULL flush to clear previous screen
}

void AssistantView::hide() {}

void AssistantView::set_audio_intensity(int intensity) { cur_audio_intensity = intensity; }

void AssistantView::start_timer(uint32_t seconds, const String& label) {
    timer_start_ms  = millis();
    timer_dur_ms    = seconds * 1000UL;
    timer_lbl       = label;
    timer_active    = true;
    timer_done_flag = false;
    timer_pct_vis   = 100.0f;
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
        if (!timer_done_flag) timer_done_flag = true;
    }

}

bool AssistantView::is_timer_done()     { return timer_done_flag; }
bool AssistantView::is_timer_active()   { return timer_active; }
float AssistantView::get_timer_vis_pct(){ return timer_pct_vis; }
const String& AssistantView::timer_label() { return timer_lbl; }

static uint16_t timer_ring_color(int pct) {
    if (pct >= 50) {
        float t = (pct - 50) / 50.0f;
        uint8_t r = (uint8_t)(31 * (1.0f - t));
        return (r << 11) | (63 << 5);
    } else {
        float t = pct / 50.0f;
        uint8_t g = (uint8_t)(63 * t);
        return (31 << 11) | (g << 5);
    }
}

void AssistantView::set_spectrum(const int* bins, int count) {
    for (int i = 0; i < 16 && i < count; i++) freq_bins[i] = bins[i];
}

// ─── Iris draw ──────────────────────────────────────────────────────────────
// Uses incremental annulus erase: only draws black over the ring that shrank
// since the last frame. No fillScreen → no 460KB PSRAM write → no DMA stall.
void AssistantView::_draw_iris(float t) {
    float base_breath = (sinf(t * 2.0f) + 1.0f) * 0.5f;
    float breath = base_breath + (audio_scale * 3.0f);
    int iris_r = 60 + (int)(breath * 25);

    uint16_t eff_glow   = C_GLOW;
    uint16_t eff_accent = C_ACCENT;
    uint16_t eff_high   = C_HIGHLIGHT;

    if (current_state == AssistantView::STATE_LISTENING) {
        eff_glow = 0xF800; eff_accent = 0xFC00; eff_high = 0xFFFF;
    } else if (current_state == AssistantView::STATE_THINKING) {
        eff_glow = 0x001F; eff_accent = 0x07FF;
    } else if (current_state == AssistantView::STATE_SPEAKING) {
        eff_glow = 0xF81F; eff_accent = 0x780F;
    } else if (current_state == AssistantView::STATE_CONTINUITY) {
        eff_glow = 0x07E0; eff_accent = 0x00FF;
        if (audio_scale < 0.1f) audio_scale = 0.1f;
    }

    if (!pi_connected) {
        eff_glow = 0x2104; // Darker grey
        eff_accent = 0x52AA; // Mid grey
        eff_high = 0x52AA; // Mid grey
    }

    int new_glow_r = iris_r + 15 + (int)(audio_scale * 30);

    // Erase only the annulus that shrank (avoids full fillScreen)
    if (av_prev_glow_r > new_glow_r) {
        for (int r = new_glow_r + 1; r <= av_prev_glow_r; r++)
            av_gfx->drawCircle(CX, CY, r, C_BG);
    }
    av_prev_glow_r = new_glow_r;

    av_gfx->fillCircle(CX, CY, new_glow_r, eff_glow);
    av_gfx->fillCircle(CX, CY, iris_r, C_SECONDARY);
    av_gfx->fillCircle(CX, CY, iris_r - 10, eff_accent);
    av_gfx->fillCircle(CX, CY, iris_r - 30, C_BG);
    av_gfx->fillCircle(CX, CY, 15 + (int)(breath * 10) + (int)(audio_scale * 40), eff_high);

    // Spectrum rings
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
            for (float off = -bar_width_deg/2.0f; off <= bar_width_deg/2.0f; off += 0.5f) {
                float rad = (angle_deg + off) * (M_PI / 180.0f);
                int x0 = CX + (int)(cosf(rad) * start_r);
                int y0 = CY + (int)(sinf(rad) * start_r);
                int x1 = CX + (int)(cosf(rad) * (start_r - mag));
                int y1 = CY + (int)(sinf(rad) * (start_r - mag));
                uint16_t c = (fabsf(off) > (bar_width_deg/2.0f - 1.0f)) ? C_SECONDARY : eff_accent;
                av_gfx->drawLine(x0, y0, x1, y1, c);
            }
        }
    }
}

// ─── Face draw ─────────────────────────────────────────────────────────────
// Incremental erase: only erases the pixels that are no longer covered by
// the new frame. Avoids the large black-rectangle flash that bounding-box
// erases cause when the DMA is scanning at 30 fps.
void AssistantView::_draw_face(float t) {
    uint32_t now = millis();

    if (current_state == AssistantView::STATE_THINKING) {
        if (now >= next_blink_ms && !is_blinking) {
            is_blinking   = true;
            blink_end_ms  = now + 140;
            next_blink_ms = now + 2500 + (rand() % 4500);
        }
    } else {
        is_blinking = false;
    }
    if (is_blinking && now >= blink_end_ms) is_blinking = false;

    float eye_spacing = 85.0f;
    float eye_y       = CY - 45.0f;
    float base_eye_r  = 48.0f;
    float breath      = (sinf(t * 1.5f) + 1.0f) * 0.5f;
    float eye_scale_v = 1.0f;
    uint16_t eye_col  = C_HIGHLIGHT;
    uint16_t face_col = C_ACCENT;
    uint16_t eye_glow = C_GLOW;
    float pulse = audio_scale * 12.0f;

    if (current_state == AssistantView::STATE_LISTENING) {
        eye_col = C_HIGHLIGHT; face_col = C_ACCENT;
    } else if (current_state == AssistantView::STATE_THINKING) {
        eye_col = 0x07FF;
    } else if (current_state == AssistantView::STATE_SPEAKING) {
        eye_col = C_ACCENT; face_col = C_ACCENT;
    } else if (current_state == AssistantView::STATE_CONTINUITY) {
        eye_scale_v = 1.1f; eye_col = 0xFF40; face_col = 0xFF40;
    }

    if (!pi_connected) {
        eye_col = 0x52AA; // Mid grey
        face_col = 0x52AA; // Mid grey
        eye_glow = 0x2104; // Darker grey
    }

    bool wink_r = (current_emotion == AssistantView::EMO_WINK);

    // ── Eyes ────────────────────────────────────────────────────────────────
    // Compute glow bbox for this frame (outermost draw = fillEllipse at rh+10, rv+8).
    // Erase using the PREVIOUS frame's bbox — prevents over-clearing on wink/blink
    // where glow can shrink from ~60 px to ~12 px in a single frame.
    int cur_l_rh, cur_l_rv, cur_r_rh, cur_r_rv;
    {
        float sv = is_blinking ? 0.05f : eye_scale_v;
        int rh = (int)(base_eye_r + pulse * 0.4f);
        int rv = (int)(base_eye_r * sv + pulse * 0.2f); if (rv < 4) rv = 4;
        cur_l_rh = rh + 10; cur_l_rv = rv + 8;
    }
    {
        float sv = (wink_r || is_blinking) ? 0.05f : eye_scale_v;
        int rh = (int)(base_eye_r + pulse * 0.4f);
        int rv = (int)(base_eye_r * sv + pulse * 0.2f); if (rv < 4) rv = 4;
        cur_r_rh = rh + 10; cur_r_rv = rv + 8;
    }
    // Use max(prev, curr)+2 to cover shrinking glow transitions and any
    // 1-pixel rasterization overshoot from fillEllipse boundary decisions.
    const int EP = 2;
    int el_rh = max(face_prev_l_rh, cur_l_rh) + EP;
    int el_rv = max(face_prev_l_rv, cur_l_rv) + EP;
    int er_rh = max(face_prev_r_rh, cur_r_rh) + EP;
    int er_rv = max(face_prev_r_rv, cur_r_rv) + EP;
    int lx = (int)(CX - eye_spacing), rx2 = (int)(CX + eye_spacing), ey_i = (int)eye_y;
    av_gfx->fillRect(lx  - el_rh, ey_i - el_rv, el_rh * 2 + 1, el_rv * 2 + 1, C_BG);
    av_gfx->fillRect(rx2 - er_rh, ey_i - er_rv, er_rh * 2 + 1, er_rv * 2 + 1, C_BG);
    face_prev_l_rh = cur_l_rh; face_prev_l_rv = cur_l_rv;
    face_prev_r_rh = cur_r_rh; face_prev_r_rv = cur_r_rv;

    auto draw_eye = [&](float x, float y, float r_base, float sv, uint16_t col, bool is_wink = false) {
        if (is_wink || is_blinking) sv = 0.05f;
        int r_h = (int)(r_base + pulse * 0.4f);
        int r_v = (int)(r_base * sv + pulse * 0.2f);
        if (r_v < 4) r_v = 4;
        av_gfx->fillEllipse(x, y, r_h + 10, r_v + 8, eye_glow);
        av_gfx->fillEllipse(x, y, r_h, r_v, col);
        av_gfx->fillEllipse(x, y, (int)(r_h * 0.55f), (int)(r_v * 0.55f), C_BG);
        if (sv > 0.40f) av_gfx->fillEllipse(x, y, r_h * 0.20f, r_v * 0.20f, col);
        if (current_emotion == AssistantView::EMO_HAPPY && !is_wink && !is_blinking) {
            for (int dx = -r_h; dx <= r_h; dx++) {
                float dy_top = -sqrtf(1.0f - powf(dx/(float)r_h, 2.0f)) * r_v;
                av_gfx->drawFastVLine(x + dx, y + dy_top, 10, col);
            }
        } else if (current_emotion == AssistantView::EMO_SARDONIC && !is_wink && !is_blinking) {
            for (int dy = -r_v; dy <= r_v; dy += 2) {
                float angle = asinf((float)abs(dy) / (float)r_v);
                int dx = (int)(cosf(angle) * r_h);
                int offset = (dy * 0.4f);
                av_gfx->drawFastHLine(x - dx + offset, y + dy, dx * 2, col);
            }
        }
    };

    draw_eye(CX - eye_spacing, eye_y, base_eye_r, eye_scale_v, eye_col);
    draw_eye(CX + eye_spacing, eye_y, base_eye_r, eye_scale_v, eye_col, wink_r);

    // ── Mouth ───────────────────────────────────────────────────────────────
    float jaw_offset = (current_state == AssistantView::STATE_SPEAKING) ? (audio_scale * 12.0f) : 0.0f;
    float mouth_y_base = CY + 80.0f + jaw_offset;

    bool is_bars = (current_state == AssistantView::STATE_LISTENING ||
                    current_state == AssistantView::STATE_SPEAKING);

    if (face_prev_was_bars != is_bars) {
        av_gfx->fillRect(CX - 95, CY + 46, 190, 82, C_BG);
        memset(face_prev_bar_h, 0, sizeof(face_prev_bar_h));
        face_prev_mouth_y = 0.0f;
        mouth_prev_n = 0;
    }
    face_prev_was_bars = is_bars;

    if (is_bars) {
        int bin_w = 12, spacing = 4;
        int total_w = (bin_w + spacing) * 12 - spacing;
        int start_x = CX - (total_w / 2);
        uint16_t bar_col = (current_state == AssistantView::STATE_LISTENING) ? C_HIGHLIGHT : C_ACCENT;
        float prev_y = (face_prev_mouth_y > 0.0f) ? face_prev_mouth_y : mouth_y_base;
        for (int i = 0; i < 12; i++) {
            float mag = freq_bins[i + 2] * 0.6f;
            if (mag > visual_bins[i]) visual_bins[i] = mag;
            else visual_bins[i] *= 0.85f;
            int bh = (int)visual_bins[i] + 4;
            int bx = start_x + i * (bin_w + spacing);
            int old_bh = (int)face_prev_bar_h[i];
            // Erase at the OLD y position (mouth_y_base shifts with jaw_offset when speaking)
            if (old_bh > 0)
                av_gfx->fillRect(bx, (int)(prev_y - old_bh / 2), bin_w, old_bh, C_BG);
            face_prev_bar_h[i] = (float)bh;
            av_gfx->fillRect(bx, (int)(mouth_y_base - bh / 2), bin_w, bh, bar_col);
        }
        face_prev_mouth_y = mouth_y_base;
    } else {
        // Wavy line: erase only the previous path segments, then draw new path.
        // Per-segment erase avoids the fillRect black-rectangle flash at 30fps.
        for (int i = 0; i + 1 < mouth_prev_n; i++) {
            av_gfx->drawLine(mouth_prev_px[i], mouth_prev_py[i],   mouth_prev_px[i+1], mouth_prev_py[i+1],   C_BG);
            av_gfx->drawLine(mouth_prev_px[i], mouth_prev_py[i]+1, mouth_prev_px[i+1], mouth_prev_py[i+1]+1, C_BG);
            av_gfx->drawLine(mouth_prev_px[i], mouth_prev_py[i]+2, mouth_prev_px[i+1], mouth_prev_py[i+1]+2, C_BG);
        }
        float mouth_w = 160.0f + (audio_scale * 20.0f);
        int prev_x = (int)(CX - (mouth_w/2.0f)), prev_y = (int)mouth_y_base;
        mouth_prev_n = 0;
        mouth_prev_px[0] = prev_x; mouth_prev_py[0] = prev_y; mouth_prev_n = 1;
        for (int x = 0; x <= (int)mouth_w; x += 6) {
            float norm_x = (x / mouth_w) * 2.0f - 1.0f;
            float curve = 0.0f;
            if (current_emotion == AssistantView::EMO_HAPPY)         curve = -22.0f * (1.0f - norm_x * norm_x);
            else if (current_emotion == AssistantView::EMO_SARDONIC)  curve = 12.0f * norm_x;
            float phase = (t * 14.0f) + (x * 0.2f);
            float amp   = (4.0f + breath * 4.0f);
            int cur_x = (int)(CX - (mouth_w/2.0f)) + x;
            int cur_y = (int)(mouth_y_base + (sinf(phase) * amp) + curve);
            av_gfx->drawLine(prev_x, prev_y,   cur_x, cur_y,   face_col);
            av_gfx->drawLine(prev_x, prev_y+1, cur_x, cur_y+1, face_col);
            av_gfx->drawLine(prev_x, prev_y+2, cur_x, cur_y+2, face_col);
            if (mouth_prev_n < 34) {
                mouth_prev_px[mouth_prev_n] = cur_x;
                mouth_prev_py[mouth_prev_n++] = cur_y;
            }
            prev_x = cur_x; prev_y = cur_y;
        }
    }

    // ── Status text: erase and redraw only when content changes ─────────────
    if (current_state != face_text_state || current_emotion != face_text_emotion) {
        av_gfx->fillRect(CX - 130, CY + 170, 280, 45, C_BG);
        av_gfx->setTextSize(2);
        av_gfx->setTextColor(C_SECONDARY);
        av_gfx->setCursor(CX - 120, CY + 175);
        const char* ms = "MODE: STANDBY";
        if (current_state == AssistantView::STATE_LISTENING)       ms = "MODE: HEURISTIC_SCAN";
        else if (current_state == AssistantView::STATE_THINKING)   ms = "MODE: PROCESSING...";
        else if (current_state == AssistantView::STATE_SPEAKING)   ms = "MODE: SYNTH_OUT";
        else if (current_state == AssistantView::STATE_CONTINUITY) ms = "MODE: CONTINUITY";
        av_gfx->print(ms);
        if (current_emotion != AssistantView::EMO_NEUTRAL) {
            av_gfx->setCursor(CX - 120, CY + 195);
            const char* es = "";
            switch (current_emotion) {
                case AssistantView::EMO_HAPPY:    es = "BIAS: POSITIVE"; break;
                case AssistantView::EMO_SARDONIC: es = "BIAS: SARDONIC"; break;
                case AssistantView::EMO_ALERT:    es = "BIAS: ALERT_PRIORITY"; break;
                case AssistantView::EMO_WINK:     es = "BIAS: GESTURE_ACK"; break;
                default: break;
            }
            av_gfx->print(es);
        }
        face_text_state   = current_state;
        face_text_emotion = current_emotion;
    }
}

void AssistantView::update() {
    if (!av_gfx) return;
    if (av_vsync_sem) {
        // Wait for VSYNC so all draws finish before the DMA scan reaches the face area.
        // After VSYNC the scan takes ~12ms to reach y=195 (eyes); our draws take ~5ms.
        xSemaphoreTake(av_vsync_sem, pdMS_TO_TICKS(50));
    } else {
        static unsigned long last_draw_ms = 0;
        unsigned long fb_now = millis();
        if (fb_now - last_draw_ms < 33) return;
        last_draw_ms = fb_now;
    }
    unsigned long now = millis();
    float t = (now - start_ms) / 1000.0f;

    float target_audio = cur_audio_intensity / 100.0f;
    if (target_audio > audio_scale) audio_scale = target_audio;
    else audio_scale *= AUDIO_DECAY;

    if (current_style == STYLE_FACE) {
        _draw_face(t);
    } else {
        _draw_iris(t);
    }

    // Timer ring — Two-phase draw to ensure clean shrinking on the canvas
    static float draw_pct = 100.0f;
    if (timer_active) {
        draw_pct += (timer_pct_vis - draw_pct) * 0.12f;
        int limit_deg = (int)(draw_pct * 3.6f);
        uint16_t col = timer_ring_color((int)draw_pct);
        int ring_r = 239, thickness = 10;

        // 1. Erase (Full 360 with 1-degree steps for absolute precision)
        for (int deg = 0; deg < 360; deg++) {
            float rad = (deg - 90) * M_PI / 180.0f;
            float cs = cosf(rad), sn = sinf(rad);
            for (int tt = -1; tt < thickness + 1; tt++) // -1 to +1 safety margin
                av_gfx->drawPixel(CX + (int)((ring_r - tt) * cs), CY + (int)((ring_r - tt) * sn), C_BG);
        }

        // 2. Draw
        for (int deg = 0; deg <= limit_deg; deg++) {
            float rad = (deg - 90) * M_PI / 180.0f;
            float cs = cosf(rad), sn = sinf(rad);
            for (int tt = 0; tt < thickness; tt++)
                av_gfx->drawPixel(CX + (int)((ring_r - tt) * cs), CY + (int)((ring_r - tt) * sn), col);
        }
    } else {
        draw_pct = 100.0f;
    }

    // ── Flush Region Selection ────────
    // If timer is active, we must flush the FULL screen for the ring (Y:0 to 480).
    // Otherwise, we only flush the active face region (Y:100 to 460) to save bandwidth.
    if (canvas && output_gfx) {
        uint16_t* buf = canvas->getFramebuffer();
        if (buf) {
            if (timer_active || current_style == STYLE_IRIS) {
                canvas->flush(); // Full flush for timer ring or full-screen IRIS
            } else {
                output_gfx->draw16bitRGBBitmap(0, 100, &buf[100 * 480], 480, 360);
            }
        }
    }
}
