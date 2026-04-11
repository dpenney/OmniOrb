#include "DiagnosticsView.h"

// ─── Palette ────────────────────────────────────────────────────────────────
static const uint16_t D_BG    = 0x0000;
static const uint16_t D_TEXT  = 0x07E0; // Matrix Green
static const uint16_t D_DIM   = 0x0120;
static const uint16_t D_ACC   = 0xE651; // Brass Gold
static const uint16_t D_MINT  = 0x97F2;

static const int CX = 240;
static const int CY = 240;

Arduino_Canvas* DiagnosticsView::canvas = nullptr;
SemaphoreHandle_t DiagnosticsView::vsync_sem = NULL;

int DiagnosticsView::mic_intensity = 0;
String DiagnosticsView::oww_status = "UNKNOWN";
String DiagnosticsView::pi_status = "DISCONNECTED";
int DiagnosticsView::wifi_rssi = 0;
String DiagnosticsView::last_wake = "NEVER";
uint32_t DiagnosticsView::last_update_ms = 0;

void DiagnosticsView::set_canvas(Arduino_Canvas *c) { canvas = c; }
void DiagnosticsView::set_vsync_sem(SemaphoreHandle_t s) { vsync_sem = s; }

void DiagnosticsView::set_mic_intensity(int i) { mic_intensity = i; last_update_ms = millis(); }
void DiagnosticsView::set_oww_status(const char* s) { oww_status = s; }
void DiagnosticsView::set_pi_status(const char* s) { pi_status = s; }
void DiagnosticsView::set_wifi_rssi(int r) { wifi_rssi = r; }
void DiagnosticsView::set_last_wake(const char* t) { last_wake = t; }

void DiagnosticsView::init() {
    last_update_ms = millis();
}

void DiagnosticsView::update() {
    if (!canvas) return;
    canvas->fillScreen(D_BG);

    // 1. Grid lines (technical look)
    for (int i = 0; i < 480; i += 40) {
        canvas->drawFastVLine(i, 0, 480, D_DIM);
        canvas->drawFastHLine(0, i, 480, D_DIM);
    }
    canvas->drawCircle(CX, CY, 220, D_DIM);

    // 2. Header
    canvas->setTextColor(D_TEXT);
    canvas->setTextSize(1);
    canvas->setCursor(CX - 80, 50);
    canvas->print("AUDIO DIAGNOSTICS [BETA]");
    canvas->drawFastHLine(CX - 100, 65, 200, D_TEXT);

    // 3. Mic VU Meter (Center Arc)
    canvas->setTextColor(D_MINT);
    canvas->setCursor(CX - 30, 110);
    canvas->print("MIC LEVEL");
    
    // Draw VU arc
    float start_angle = 150.0f;
    float span = 240.0f;
    float pct = mic_intensity / 100.0f;
    
    for (float a = 0; a < span; a += 1.0f) {
        float angle = (start_angle + a) * (M_PI / 180.0f);
        uint16_t col = (a < span * pct) ? D_MINT : D_DIM;
        int r1 = 160, r2 = 180;
        canvas->drawLine(
            CX + cosf(angle) * r1, CY + sinf(angle) * r1,
            CX + cosf(angle) * r2, CY + sinf(angle) * r2,
            col
        );
    }
    
    // 4. Intensity Number
    canvas->setTextSize(3);
    canvas->setTextColor(0xFFFF);
    canvas->setCursor(CX - 25, CY - 15);
    canvas->printf("%02d", mic_intensity);
    canvas->setTextSize(1);
    canvas->setCursor(CX + 15, CY + 5);
    canvas->print("%");

    // 5. System Status Block
    int y = 280;
    canvas->setTextColor(D_ACC);
    canvas->setCursor(70, y); canvas->print("PI BACKEND: ");
    canvas->setTextColor(0xFFFF); canvas->print(pi_status);

    y += 20;
    canvas->setTextColor(D_ACC);
    canvas->setCursor(70, y); canvas->print("WAKE WORD:  ");
    canvas->setTextColor(0xFFFF); canvas->print(oww_status);

    y += 20;
    canvas->setTextColor(D_ACC);
    canvas->setCursor(70, y); canvas->print("LAST WAKE:  ");
    canvas->setTextColor(0xFFFF); canvas->print(last_wake);

    y += 20;
    canvas->setTextColor(D_ACC);
    canvas->setCursor(70, y); canvas->print("WIFI RSSI:  ");
    canvas->setTextColor(0xFFFF); canvas->printf("%d dBm", wifi_rssi);

    // 6. Footer / Heartbeat
    uint32_t age = (millis() - last_update_ms) / 1000;
    canvas->setTextColor(D_DIM);
    canvas->setCursor(CX - 75, 420);
    canvas->printf("DATA AGE: %d SECONDS", age);
    
    canvas->setCursor(CX - 84, 445);
    canvas->print("SWIPE RIGHT TO EXIT");

    if (vsync_sem) xSemaphoreTake(vsync_sem, portMAX_DELAY);
    canvas->flush();
}
