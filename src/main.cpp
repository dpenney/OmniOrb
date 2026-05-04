/**
 * @file main.cpp
 * @brief ADS-B Radar Display — Waveshare ESP32-S3-Knob-Touch-LCD-1.8
 *
 * Key design choices
 * ──────────────────
 * • HTTP fetch runs on Core 0 (FreeRTOS task) — the sweep animation on Core 1
 *   never blocks, so the arm moves smoothly even during network calls.
 * • A mutex protects the shared aircraft array.
 * • PPI-style painting: blips appear only when the sweep arm crosses their
 *   bearing; on the next pass the arm erases and repaints with fresh data.
 * • Labels are erased by redrawing the exact text in black, not a dumb
 *   fillRect, so nearby labels are not clobbered.
 *
 * Controls:
 *   Encoder CW/CCW  — zoom in / zoom out
 *   Touch blip      — show aircraft detail
 *   Touch elsewhere — dismiss detail
 */

#include <Arduino.h>
#include <atomic>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <esp_heap_caps.h>
#define private public
#define protected public
#include <Arduino_GFX_Library.h>
#undef private
#undef protected
#include <math.h>

#include "pins.h"
#include "config.h"
#include "waveshare_init.h"
#include "Settings.h"
#include "Provisioning.h"
#include "ClockView.h"
#include "AssistantView.h"
#include "SettingsView.h"
#include "DiagnosticsView.h"
#include "GlobeView.h"
#include <time.h>
#include <ArduinoOTA.h>


#include <lvgl.h>

ProjectSettings settings;
bool pi_connected = false;
uint32_t last_pi_msg_ms = 0;

enum AppState { APP_RADAR, APP_ASSISTANT, APP_CLOCK, APP_GLOBE, APP_SETTINGS, APP_DIAGNOSTICS };
static AppState current_app = APP_RADAR;
static AppState prev_app    = APP_RADAR;   // where to return after settings
static Arduino_Canvas *assistant_canvas = nullptr;




// ─── Colours (RGB565: RRRRR GGGGGG BBBBB) ────────────────────────────────────
static const uint16_t C_BG       = 0x0000;  // black
static const uint16_t C_RING     = 0x0120;  // dark green ring
static const uint16_t C_GRID     = 0x00A0;  // dim green crosshair
static const uint16_t C_SWEEP    = 0x07E0;  // bright green sweep arm
static const uint16_t C_BLIP     = 0x07E0;  // blip colour
static const uint16_t C_DIM_BLIP = 0x02E0;  // dimmed blip (dark green)
static const uint16_t C_SEL      = 0xFFFF;  // selected aircraft (white)
static const uint16_t C_LBL      = 0x03E0;  // callsign label
static const uint16_t C_BOX_BG   = 0x0020;  // detail box background
static const uint16_t C_BOX_BORD = 0x03E0;  // detail box border

// ─── Display & Expanders ─────────────────────────────────────────────────────

#include "TCA9554PWR.h"
#include "Touch_GT911.h"

TCA9554PWR io_expander(TCA9554_ADDR);
Arduino_RGB_Display *gfx = nullptr; // Initialized in setup via create_waveshare_28C_rgb_panel()

#define SCREEN_WIDTH  480
#define SCREEN_HEIGHT 480
#define CX            240
#define CY            240
#define SCREEN_RADIUS 230

static const float DEG2RAD    = M_PI / 180.0f;
static const float NM_PER_DEG = 60.0f;

void full_redraw(); 
int  find_nearest(int x, int y);
void draw_blip_shape(int x, int y, int head, uint16_t color);
void draw_detail_box();
void erase_detail_box();

static bool detail_visible = false;
static bool detail_clobbered = false;
static float range_nm      = DEFAULT_RANGE_NM;
static int   selected_idx  = -1;

// ─── Touch ───────────────────────────────────────────────────────────────────

Touch_GT911 touch;

void touch_init() {
    // Touch reset is handled inside create_waveshare_28C_rgb_panel() via TCA9554
    pinMode(TOUCH_INT, INPUT); // GT911 Interrupt pin
    if (touch.begin()) {
        Serial.println("GT911 Touch initialized successfully.");
    }
}

static int  touch_x = -1, touch_y = -1;

// We will track the last touch state to handle tap vs swipe/zoom
static bool last_was_touching = false;
static bool touch_active = false;
static uint32_t last_touch_time = 0;
static int touch_start_x = -1, touch_start_y = -1;

bool read_touch() {
    if (touch.read()) {
        // We actually got a status update from the hardware!
        if (touch.points > 0) {
            touch_x = touch.touches[0].x;
            touch_y = touch.touches[0].y;
            touch_active = true;
        } else {
            touch_active = false;
        }
    }
    // If touch.read() returned false, there was no hardware update; 
    // keep the old touch_active/last_was_touching state.
    return touch_active;
}


// ─── Aircraft (shared between Core 0 fetch and Core 1 render) ────────────────

#define MAX_AIRCRAFT 64

Aircraft aircraft[MAX_AIRCRAFT];
int      aircraft_count = 0;

// Mutex protecting aircraft[] and aircraft_count
SemaphoreHandle_t ac_mutex;

// ─── Gesture Detection ───────────────────────────────────────────────────────
#define GESTURE_THRESHOLD 10
#define TAP_THRESHOLD     20   ///< Max dx+dy to treat as a tap

// ─── Detail Box Layout ────────────────────────────────────────────────────────
#define DETAIL_BX  160
#define DETAIL_BY   40
#define DETAIL_BW  160
#define DETAIL_BH   68


static bool lvgl_active = false; // True only when LVGL clock screen is the active view

void notify_pi_settings() {
    // Send location + timezone so Pi can use them for weather, ADS-B bounding box, etc.
    String msg = String("GEO:") + String(settings.home_lat, 6) + "," +
                 String(settings.home_lon, 6) + "," + String(settings.timezone);
    Serial0.println(msg);
    Serial.printf("[UART→Pi] %s\n", msg.c_str());

    // Also sync current volume
    String vmsg = String("VOL:") + String(settings.volume);
    Serial0.println(vmsg);
    Serial.printf("[UART→Pi] %s\n", vmsg.c_str());
}

void notify_pi_app_mode(AppState mode) {
    String modeStr = "OTHER";
    if (mode == APP_ASSISTANT) modeStr = "ASSISTANT";
    else if (mode == APP_CLOCK) modeStr = "CLOCK";
    else if (mode == APP_RADAR) modeStr = "RADAR";
    else if (mode == APP_GLOBE) modeStr = "GLOBE";
    else if (mode == APP_SETTINGS) modeStr = "SETTINGS";
    else if (mode == APP_DIAGNOSTICS) modeStr = "DIAGNOSTICS";
    
    String msg = "APP:" + modeStr;
    Serial0.println(msg);                          // Hardware UART0 → Pi
    Serial.printf("[UART→Pi] %s\n", msg.c_str());  // USB monitor visibility
}

// ─── Settings entry / exit ────────────────────────────────────────────────────
void enter_settings() {
    prev_app = current_app;
    if (current_app == APP_CLOCK) {
        lvgl_active = false;
        lv_obj_t *blank = lv_obj_create(NULL);
        lv_obj_set_style_bg_color(blank, lv_color_black(), 0);
        lv_scr_load(blank);
    }
    current_app = APP_SETTINGS;
    SettingsView::show();
    SettingsView::update();   // draw immediately on entry
}

void exit_settings() {
    // Sync volume to Pi before leaving
    String vmsg = String("VOL:") + String(settings.volume);
    Serial0.println(vmsg);
    Serial.printf("[UART→Pi] %s\n", vmsg.c_str());

    // Safety: If prev_app is a menu, default to Radar
    if (prev_app == APP_SETTINGS || prev_app == APP_DIAGNOSTICS) {
        prev_app = APP_RADAR;
    }

    Serial0.printf("Exiting Settings: Returning to prev_app ID %d\n", (int)prev_app);
    current_app = prev_app;
    Serial0.printf("Exiting Settings: current_app is now ID %d\n", (int)current_app);

    if (current_app == APP_CLOCK) {
        lvgl_active = true;
        ClockView::show();
    } else if (current_app == APP_ASSISTANT) {
        AssistantView::show();
    } else if (current_app == APP_GLOBE) {
        GlobeView::show();
    } else if (current_app == APP_DIAGNOSTICS) {
        DiagnosticsView::init();
    } else {
        // Default: APP_RADAR
        current_app = APP_RADAR;
        full_redraw();
    }
    notify_pi_app_mode(current_app);
}

void process_swipe(int x1, int y1, int x2, int y2) {
    int dx = x2 - x1;
    int dy = y2 - y1;

    // ── Settings view: swipe right exits; arc drag handled in loop() ─────────
    // ── Settings/Diagnostics view: swipe right exits; arc drag handled in loop() ──
    if (current_app == APP_SETTINGS || current_app == APP_DIAGNOSTICS) {
        if (abs(dx) > abs(dy) && dx > GESTURE_THRESHOLD) {
            if (current_app == APP_DIAGNOSTICS) {
                current_app = APP_SETTINGS;
                // SettingsView update happens in next frame
            } else {
                exit_settings();
            }
        }
        return;
    }

    if (abs(dx) < TAP_THRESHOLD && abs(dy) < TAP_THRESHOLD) {
        if (current_app == APP_RADAR) {
            int hit = find_nearest(x2, y2);
        if (hit >= 0 && hit < aircraft_count) {
            selected_idx = hit;
            if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
                Aircraft &ac = aircraft[hit];
                if (ac.paint_valid)
                    draw_blip_shape(ac.paint_x, ac.paint_y, ac.heading, C_SEL);
                xSemaphoreGive(ac_mutex);
            }
            draw_detail_box();
        } else if (detail_visible) {
            selected_idx = -1;
            erase_detail_box();
        }
    }
        return;
    }

    // Vertical Swipes
    if (abs(dy) > abs(dx)) {
        if (current_app == APP_ASSISTANT) {
            if (abs(dy) > GESTURE_THRESHOLD) {
                AssistantView::toggle_style();
                Serial.println("Gesture: SWIPE (Switch Assistant Style)");
            }
        } else if (current_app == APP_RADAR) {
            if (dy < -GESTURE_THRESHOLD) {
                // Swipe UP -> Zoom IN
                range_nm = max((float)MIN_RANGE_NM, range_nm / 1.3f);
                Serial.println("Gesture: SWIPE UP (Zoom IN)");
                full_redraw();
            } else if (dy > GESTURE_THRESHOLD) {
                // Swipe DOWN -> Zoom OUT
                range_nm = min((float)MAX_RANGE_NM, range_nm * 1.3f);
                Serial.println("Gesture: SWIPE DOWN (Zoom OUT)");
                full_redraw();
            }
        }
    } 
    // Horizontal Swipes (App Navigation Placeholder)
    else {
        if (dx < -GESTURE_THRESHOLD) {
            Serial.println("Gesture: SWIPE LEFT (Radar -> Globe -> Assistant -> Clock)");
            if (current_app == APP_RADAR) {
                current_app = APP_GLOBE;
                notify_pi_app_mode(current_app);
                GlobeView::show();
            } else if (current_app == APP_GLOBE) {
                current_app = APP_ASSISTANT;
                AssistantView::show();
                notify_pi_app_mode(current_app);
            } else if (current_app == APP_ASSISTANT) {
                current_app = APP_CLOCK;
                notify_pi_app_mode(current_app);
                lvgl_active = true;  // Enable LVGL flush before showing clock screen
                ClockView::show();
            } else if (current_app == APP_CLOCK) {
                current_app = APP_RADAR;
                notify_pi_app_mode(current_app);
                lvgl_active = false;
                full_redraw();
            }
        } else if (dx > GESTURE_THRESHOLD) {
            Serial.println("Gesture: SWIPE RIGHT (Clock -> Assistant -> Globe -> Radar)");
            if (current_app == APP_CLOCK) {
                lvgl_active = false;  // Stop LVGL from flushing over radar
                current_app = APP_ASSISTANT;
                AssistantView::show();
                notify_pi_app_mode(current_app);
                // Load a blank black screen so LVGL has something safe to reference
                lv_obj_t *blank = lv_obj_create(NULL);
                lv_obj_set_style_bg_color(blank, lv_color_black(), 0);
                lv_scr_load(blank);
            } else if (current_app == APP_ASSISTANT) {
                current_app = APP_GLOBE;
                notify_pi_app_mode(current_app);
                GlobeView::show();
            } else if (current_app == APP_GLOBE) {
                current_app = APP_RADAR;
                notify_pi_app_mode(current_app);
                full_redraw();
            }
        }



    }
}


float bearing_to(float lat, float lon) {
    float dlat = lat - settings.home_lat;
    float dlon = (lon - settings.home_lon) * cosf(settings.home_lat * DEG2RAD);
    float b = atan2f(dlon, dlat) * (180.0f / M_PI);
    return b < 0 ? b + 360.0f : b;
}

bool latlon_to_screen(float lat, float lon, int *sx, int *sy) {
    float dlat = lat - settings.home_lat;
    float dlon = (lon - settings.home_lon) * cosf(settings.home_lat * DEG2RAD);
    float dist = sqrtf(dlat*dlat + dlon*dlon) * NM_PER_DEG;
    if (dist > range_nm) return false;
    float scale = (float)SCREEN_RADIUS / range_nm;
    *sx = (int)(CX + dlon * NM_PER_DEG * scale);
    *sy = (int)(CY - dlat * NM_PER_DEG * scale);
    return true;
}

// ─── Core 0 Fetch Task ───────────────────────────────────────────────────────

// ─── LVGL Integration ────────────────────────────────────────────────────────
static const uint32_t screenWidth  = SCREEN_WIDTH;
static const uint32_t screenHeight = SCREEN_HEIGHT;
static lv_disp_draw_buf_t draw_buf;
static lv_color_t *disp_draw_buf;
static lv_disp_drv_t disp_drv;



// ─── LVGL VSnyc Synchronization ────────────────────────────────────────────────
// To prevent tearing, we align the LVGL buffer flush with the ESP32 LCD driver's
// VSYNC signal. This callback gives a semaphore exactly when the screen finishes
// drawing a frame, so our flush memcpy happens safely in the blanking period.
// ─────────────────────────────────────────────────────────────────────────────
SemaphoreHandle_t vsync_sem = NULL;

IRAM_ATTR bool example_lvgl_on_vsync_callback(esp_lcd_panel_handle_t panel, const esp_lcd_rgb_panel_event_data_t *event_data, void *user_data)
{
    BaseType_t high_task_awoken = pdFALSE;
    if (vsync_sem) {
        xSemaphoreGiveFromISR(vsync_sem, &high_task_awoken);
    }
    return high_task_awoken == pdTRUE;
}

void my_disp_flush(lv_disp_drv_t *disp_drv, const lv_area_t *area, lv_color_t *color_p) {
    if (lvgl_active) {
        uint32_t w = (area->x2 - area->x1 + 1);
        uint32_t h = (area->y2 - area->y1 + 1);
        
        // Wait for VSYNC immediately before writing to the display to prevent tearing.
        // This ensures the render time of lv_timer_handler doesn't push the DMA write
        // into the middle of the display's active scanout.
        if (vsync_sem) {
            xSemaphoreTake(vsync_sem, 0); // Drain stale token
            xSemaphoreTake(vsync_sem, pdMS_TO_TICKS(100));
        }
        
        gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)&color_p->full, w, h);
    }
    lv_disp_flush_ready(disp_drv);
}

void my_touchpad_read(lv_indev_drv_t *indev_drv, lv_indev_data_t *data) {
    if (touch_x != -1 && touch_y != -1 && touch_active) {
        data->state = LV_INDEV_STATE_PR;
        data->point.x = touch_x;
        data->point.y = touch_y;
    } else {
        data->state = LV_INDEV_STATE_REL;
    }
}
// ─────────────────────────────────────────────────────────────────────────────

static std::atomic<bool> fetch_requested{false};
static std::atomic<bool> fetch_busy{false};

void fetch_task(void *pv) {
    for (;;) {
        if (fetch_requested && !fetch_busy) {
            fetch_busy      = true;
            fetch_requested = false;

            if (WiFi.status() == WL_CONNECTED) {
                HTTPClient http;
                String url = String("http://") + ADSB_HOST + ":" + ADSB_PORT + ADSB_PATH + 
                             "?lat=" + String(settings.home_lat, 4) + 
                             "&lon=" + String(settings.home_lon, 4) +
                             "&range=" + String(range_nm, 1);
                http.begin(url);
                Serial0.printf("Fetching: %s\n", url.c_str());
                http.setTimeout(2500);
                int code = http.GET();
                if (code == 200) {
                    // Parse into a local temporary buffer
                    JsonDocument doc;
                    deserializeJson(doc, *http.getStreamPtr());
                    http.end();

                    // Update aircraft list in-place
                    xSemaphoreTake(ac_mutex, portMAX_DELAY);
                    for (JsonObject ac_obj : doc["aircraft"].as<JsonArray>()) {
                        const char *hex = ac_obj["hex"] | "";
                        if (!hex[0]) continue;

                        // Find existing or empty slot
                        int idx = -1;
                        for (int i = 0; i < aircraft_count; i++) {
                            if (strcmp(aircraft[i].hex, hex) == 0) { idx = i; break; }
                        }
                        if (idx == -1 && aircraft_count < MAX_AIRCRAFT) {
                            idx = aircraft_count++;
                            memset(&aircraft[idx], 0, sizeof(Aircraft));
                            strlcpy(aircraft[idx].hex, hex, sizeof(aircraft[idx].hex));
                        }

                        if (idx != -1) {
                            Aircraft &a = aircraft[idx];
                            const char *fl = ac_obj["flight"];
                            if (fl) strlcpy(a.callsign, fl, sizeof(a.callsign));
                            else if (!a.callsign[0]) strlcpy(a.callsign, a.hex, sizeof(a.callsign));
                            for (int i=strlen(a.callsign)-1;i>=0&&a.callsign[i]==' ';i--) a.callsign[i]='\0';
                            
                            a.has_pos  = ac_obj["lat"].is<float>() && ac_obj["lon"].is<float>();
                            if (a.has_pos) {
                                a.lat      = ac_obj["lat"];
                                a.lon      = ac_obj["lon"];
                                a.bearing  = bearing_to(a.lat, a.lon);
                                // Mark fresh only if updated in last 5 seconds
                                if ((float)(ac_obj["seen_pos"] | 0.0f) < 5.0f) {
                                    a.position_updated = true;
                                }
                            }
                            a.altitude = ac_obj["alt_baro"] | a.altitude;
                            a.speed    = ac_obj["gs"].is<float>() ? (int)ac_obj["gs"].as<float>() : a.speed;
                            a.heading  = ac_obj["track"].is<float>() ? (int)ac_obj["track"].as<float>() : a.heading;
                            a.seen_ms  = millis();
                        }
                    }
                    xSemaphoreGive(ac_mutex);
                } else {
                    http.end();
                }
            }
            fetch_busy = false;
        }
        vTaskDelay(10 / portTICK_PERIOD_MS);
    }
}

// ─── Rendering (Core 1) ───────────────────────────────────────────────────────

static float sweep_angle = 0.0f;
static float prev_sweep  = -1.0f;

void draw_blip_shape(int cx, int cy, int hdg, uint16_t col) {
    float a = hdg * DEG2RAD, sz = 6;
    int x0=cx+(int)(sinf(a)*sz),            y0=cy-(int)(cosf(a)*sz);
    int x1=cx+(int)(sinf(a+2.4f)*sz*.7f),   y1=cy-(int)(cosf(a+2.4f)*sz*.7f);
    int x2=cx+(int)(sinf(a-2.4f)*sz*.7f),   y2=cy-(int)(cosf(a-2.4f)*sz*.7f);
    gfx->drawTriangle(x0,y0,x1,y1,x2,y2,col);
}

void restore_rings_and_cross() {
    for (int r = 1; r <= 3; r++)
        gfx->drawCircle(CX, CY, SCREEN_RADIUS*r/3, C_RING);
    gfx->drawFastHLine(CX-SCREEN_RADIUS, CY, SCREEN_RADIUS*2, C_GRID);
    gfx->drawFastVLine(CX, CY-SCREEN_RADIUS, SCREEN_RADIUS*2, C_GRID);
    gfx->fillCircle(CX, CY, 3, C_BLIP);
    // Restore range labels at 12-o'clock on each ring
    gfx->setTextColor(C_LBL, C_BG);
    gfx->setTextSize(1);
    char buf[8];
    for (int r = 1; r <= 3; r++) {
        sprintf(buf, "%.0f", range_nm*r/3.0f);
        int16_t x, y; uint16_t w, h;
        gfx->getTextBounds(buf, 0, 0, &x, &y, &w, &h);
        gfx->setCursor(CX - w/2, CY - SCREEN_RADIUS*r/3 + 1);
        gfx->print(buf);
    }
}

void draw_static_bg() {
    gfx->fillScreen(C_BG);
    restore_rings_and_cross();
}

/** True if segment (x1,y1)→(x2,y2) passes within `thresh` pixels of point (px,py) */
static bool line_near(int x1, int y1, int x2, int y2, int px, int py, float thresh) {
    float dx = x2-x1, dy = y2-y1;
    float len2 = dx*dx + dy*dy;
    if (len2 < 1.0f) return false;
    // Project point onto segment, clamp to [0,1]
    float t = ((px-x1)*dx + (py-y1)*dy) / len2;
    if (t < 0.0f) t = 0.0f;
    if (t > 1.0f) t = 1.0f;
    float cx = x1 + t*dx - px;
    float cy = y1 + t*dy - py;
    return (cx*cx + cy*cy) < thresh*thresh;
}

void erase_sweep(float a_deg) {
    float a  = a_deg * DEG2RAD;
    int   ex = CX + (int)(sinf(a) * SCREEN_RADIUS);
    int   ey = CY - (int)(cosf(a) * SCREEN_RADIUS);
    gfx->drawLine(CX, CY, ex, ey, C_BG);
    restore_rings_and_cross();

    // Restore any painted blips the sweep line crossed through
    if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
    for (int i = 0; i < aircraft_count; i++) {
        Aircraft &ac = aircraft[i];
        if (!ac.paint_valid) continue;

        // Check triangle centre and several points along the callsign label
        int  lx = ac.paint_x, ly = ac.paint_y;
        int  label_len = strlen(ac.paint_cs);
        bool hit = line_near(CX, CY, ex, ey, lx, ly, 10.0f);   // blip triangle
        for (int c = 0; c <= label_len && !hit; c++) {           // scan label chars
            hit = line_near(CX, CY, ex, ey, lx+8+c*6, ly, 7.0f);
        }
        if (!hit) continue;

        // Redraw this blip
        bool sel = (i == selected_idx);
        draw_blip_shape(lx, ly, ac.paint_hdg, sel ? C_SEL : (ac.is_dimmed ? C_DIM_BLIP : C_BLIP));
        gfx->setTextColor(sel ? C_SEL : C_LBL, C_BG);
        gfx->setTextSize(1);
        gfx->setCursor(lx+8, ly-4);
        gfx->print(ac.paint_cs);
    }
        xSemaphoreGive(ac_mutex);
    }

    // If detail box is visible, check if the sweep line clobbers it
    if (detail_visible) {
        // Box is at (160, 40) size (160, 68)
        // Check if (CX, CY) -> (ex, ey) passes near box
        // For simplicity, check 4 corners and center
        if (line_near(CX, CY, ex, ey, DETAIL_BX,            DETAIL_BY,            10.0f) ||
            line_near(CX, CY, ex, ey, DETAIL_BX+DETAIL_BW,  DETAIL_BY,            10.0f) ||
            line_near(CX, CY, ex, ey, DETAIL_BX,            DETAIL_BY+DETAIL_BH,  10.0f) ||
            line_near(CX, CY, ex, ey, DETAIL_BX+DETAIL_BW,  DETAIL_BY+DETAIL_BH,  10.0f) ||
            line_near(CX, CY, ex, ey, DETAIL_BX+DETAIL_BW/2, DETAIL_BY+DETAIL_BH/2, 10.0f)) {
            detail_clobbered = true;
        }
    }
}

void draw_sweep(float a_deg) {
    float a = a_deg * DEG2RAD;
    gfx->drawLine(CX, CY, CX+(int)(sinf(a)*SCREEN_RADIUS), CY-(int)(cosf(a)*SCREEN_RADIUS), C_SWEEP);
}


/** Erase a blip — fillRect over icon area (robust regardless of heading), then exact text overdraw */
void erase_blip(int x, int y, int hdg, const char *cs) {
    // Wipe a box big enough to cover the triangle in any orientation
    gfx->fillRect(x-9, y-9, 18, 18, C_BG);
    // Erase label by overdrawing exact text in black
    gfx->setTextColor(C_BG, C_BG);
    gfx->setTextSize(1);
    gfx->setCursor(x+8, y-4);
    gfx->print(cs);
    restore_rings_and_cross();
}

/** Paint a blip and record paint state */
void paint_blip(Aircraft &ac, int sx, int sy, uint16_t color) {
    bool sel = (&ac - aircraft) == selected_idx;
    draw_blip_shape(sx, sy, ac.heading, sel ? C_SEL : color);
    gfx->setTextColor(sel ? C_SEL : C_LBL, C_BG);
    gfx->setTextSize(1);
    gfx->setCursor(sx+8, sy-4);
    gfx->print(ac.callsign);
    ac.paint_x   = sx;
    ac.paint_y   = sy;
    ac.paint_hdg = ac.heading;
    strlcpy(ac.paint_cs, ac.callsign, sizeof(ac.paint_cs));
    ac.paint_valid = true;
}

/**
 * For each aircraft whose bearing is crossed by the sweep arm advancing
 * from prev_angle to new_angle: erase old painted position, repaint new.
 */
void sweep_paint_aircraft(float prev_angle, float new_angle) {
    if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(10)) != pdTRUE) return;

    for (int i = 0; i < aircraft_count; i++) {
        Aircraft &ac = aircraft[i];
        if (!ac.has_pos || ac.bearing < 0) continue;
        
        // Hard expiry for very old data
        if (millis() - ac.seen_ms > (uint32_t)(AIRCRAFT_MAX_AGE_S * 1000)) {
            if (ac.paint_valid) {
                erase_blip(ac.paint_x, ac.paint_y, ac.heading, ac.paint_cs);
                ac.paint_valid = false;
            }
            continue;
        }

        bool crossed = (prev_angle <= new_angle)
            ? (ac.bearing >= prev_angle && ac.bearing < new_angle)
            : (ac.bearing >= prev_angle || ac.bearing < new_angle);
        if (!crossed) continue;

        // Erase old painted position (if any)
        if (ac.paint_valid)
            erase_blip(ac.paint_x, ac.paint_y, ac.heading, ac.paint_cs);

        // Aging logic
        if (ac.position_updated) {
            // Fresh data! Paint normally.
            int sx, sy;
            if (latlon_to_screen(ac.lat, ac.lon, &sx, &sy)) {
                paint_blip(ac, sx, sy, C_BLIP);
                ac.is_dimmed = false;
            } else {
                ac.paint_valid = false;
            }
            ac.position_updated = false;
        } else {
            // No update since last sweep
            if (ac.is_dimmed) {
                // Was already dimmed, now remove
                ac.paint_valid = false;
            } else {
                // Dim it
                int sx, sy;
                if (latlon_to_screen(ac.lat, ac.lon, &sx, &sy)) {
                    paint_blip(ac, sx, sy, C_DIM_BLIP);
                    ac.is_dimmed = true;
                } else {
                    ac.paint_valid = false;
                }
            }
        }
    }

        xSemaphoreGive(ac_mutex);
}

void draw_detail_box() {
    if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(10)) != pdTRUE) return;
    if (selected_idx < 0 || selected_idx >= aircraft_count) {
        xSemaphoreGive(ac_mutex); return;
    }
    Aircraft ac = aircraft[selected_idx];  // local copy
    xSemaphoreGive(ac_mutex);

    gfx->fillRect(DETAIL_BX, DETAIL_BY, DETAIL_BW, DETAIL_BH, C_BOX_BG);
    gfx->drawRect(DETAIL_BX, DETAIL_BY, DETAIL_BW, DETAIL_BH, C_BOX_BORD);
    gfx->setTextColor(C_LBL, C_BOX_BG);
    gfx->setTextSize(1);
    gfx->setCursor(DETAIL_BX+6, DETAIL_BY+8);  gfx->print(ac.callsign[0] ? ac.callsign : ac.hex);
    gfx->setCursor(DETAIL_BX+6, DETAIL_BY+22); gfx->printf("Alt: %d ft", ac.altitude);
    gfx->setCursor(DETAIL_BX+6, DETAIL_BY+36); gfx->printf("Spd: %d kts", ac.speed);
    gfx->setCursor(DETAIL_BX+6, DETAIL_BY+50); gfx->printf("Hdg: %d deg", ac.heading);
    detail_visible = true;
    detail_clobbered = false;
    xSemaphoreGive(ac_mutex);
}

void erase_detail_box() {
    gfx->fillRect(DETAIL_BX, DETAIL_BY, DETAIL_BW, DETAIL_BH, C_BG);
    restore_rings_and_cross();
    detail_visible = false;
    detail_clobbered = false;
}

void draw_range_label() {
    gfx->fillRect(CX-40, SCREEN_HEIGHT-18, 80, 14, C_BG);
    gfx->setTextColor(C_LBL, C_BG);
    gfx->setTextSize(1);
    gfx->setCursor(CX-30, SCREEN_HEIGHT-14);
    gfx->printf("%.0f nm", range_nm);
}

int find_nearest(int tx, int ty) {
    int best = -1; float best_d = 18.0f;
    if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(10)) != pdTRUE) return -1;
    for (int i = 0; i < aircraft_count; i++) {
        if (!aircraft[i].paint_valid) continue;
        float d = sqrtf((float)((tx-aircraft[i].paint_x)*(tx-aircraft[i].paint_x)+
                                (ty-aircraft[i].paint_y)*(ty-aircraft[i].paint_y)));
        if (d < best_d) { best_d=d; best=i; }
    }
    xSemaphoreGive(ac_mutex);
    return best;
}

// Draw timer ring on the radar/gfx canvas — safe at r=231-240 since sweep only reaches r=230
static void draw_radar_timer_ring() {
    const float STEP = 0.008f;
    const float FULL = 2.0f * M_PI;
    const float start_rad = -M_PI / 2.0f;

    if (!AssistantView::is_timer_active()) {
        // Erase any leftover ring
        for (float a = start_rad; a <= start_rad + FULL; a += STEP) {
            float cs = cosf(a), sn = sinf(a);
            for (int tt = 0; tt < 10; tt++)
                gfx->drawPixel(CX + (int)((238 - tt) * cs), CY + (int)((238 - tt) * sn), C_BG);
        }
        return;
    }

    float pct      = AssistantView::get_timer_vis_pct();
    float span_rad = (pct / 100.0f) * FULL;
    float end_rad  = start_rad + span_rad;

    // Green → yellow → red colour
    uint16_t col;
    int ipct = (int)pct;
    if (ipct >= 50) {
        float t  = (ipct - 50) / 50.0f;
        uint8_t r = (uint8_t)(31 * (1.0f - t));
        col = (r << 11) | (63 << 5);
    } else {
        float t  = ipct / 50.0f;
        uint8_t g = (uint8_t)(63 * t);
        col = (31 << 11) | (g << 5);
    }

    // Draw full ring: coloured arc then background for the remainder
    for (float a = start_rad; a <= start_rad + FULL; a += STEP) {
        uint16_t px_col = (a <= end_rad) ? col : C_BG;
        float cs = cosf(a), sn = sinf(a);
        for (int tt = 0; tt < 10; tt++)
            gfx->drawPixel(CX + (int)((238 - tt) * cs), CY + (int)((238 - tt) * sn), px_col);
    }
}

void full_redraw() {
    // Invalidate all paint state so aircraft repaint on next sweep pass
    if (xSemaphoreTake(ac_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        for (int i = 0; i < aircraft_count; i++) aircraft[i].paint_valid = false;
        xSemaphoreGive(ac_mutex);
    } else {
        Serial.println("Warning: ac_mutex held during full_redraw!");
    }
    draw_static_bg();
    draw_radar_timer_ring();   // drawn after bg, outside sweep radius — will persist
    draw_sweep(sweep_angle);
    draw_range_label();
    if (detail_visible) draw_detail_box();
    prev_sweep = sweep_angle;
}

// ─── Arduino entry points ─────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    Serial0.begin(115200); // Hardware UART0 (usually on header pins 43/44)
    // Encoder removed.
    
    // PWM Backlight on native GPIO 6
    // The demo uses 20kHz, 10-bit resolution. 100% duty = 1024
    ledcAttach(LCD_BL, 20000, 10);
    ledcWrite(LCD_BL, 1023); // Max brightness (100%)

    vsync_sem = xSemaphoreCreateBinary();

    gfx = create_waveshare_28C_rgb_panel();
    bool gfx_ok = gfx->begin();
    Serial.printf("gfx->begin() = %s\n", gfx_ok ? "OK" : "FAILED");

    // If display init failed, halt here rather than crashing in VSYNC registration
    if (!gfx_ok) {
        Serial.println("FATAL: gfx begin failed — halting");
        for (;;) delay(1000);
    }

#ifdef DISPLAY_TEST_MODE
    // Bare-bones diagnostic: direct draw, no WiFi/canvas/LVGL/tasks.
    // If flicker is still visible here, the cause is hardware or display timing.
    gfx->fillScreen(0x0000);
    gfx->setTextColor(0xFFFF, 0x0000);
    gfx->setTextSize(3);
    gfx->setCursor(60, 160); gfx->print("DISPLAY TEST");
    gfx->setTextSize(2);
    gfx->setCursor(60, 210); gfx->print("No WiFi");
    gfx->setCursor(60, 235); gfx->print("No canvas");
    gfx->setCursor(60, 260); gfx->print("No LVGL");
    gfx->setCursor(60, 285); gfx->print("Direct GFX only");
    for (;;) delay(100);
#endif

    // ─── VSYNC Callback Registration Hack ───
    // Arduino_GFX abstracts the ESP32-S3 RGB LCD driver, keeping `_panel_handle`
    // inaccessible. We redefine private to public in the header to access the handle
    // and manually register the VSYNC interrupt that LVGL depends on.
    Serial.println("Registering VSYNC callback...");
    esp_lcd_rgb_panel_event_callbacks_t cbs = {
        .on_vsync = example_lvgl_on_vsync_callback,
    };
    esp_lcd_rgb_panel_register_event_callbacks(gfx->_rgbpanel->_panel_handle, &cbs, NULL);
    Serial.println("VSYNC registered OK");

    touch_init();

    // Splash screen while WiFi connects
    gfx->fillScreen(C_BG);
    gfx->setTextColor(C_SWEEP, C_BG);
    gfx->setTextSize(2);
    gfx->setCursor(45, 155); gfx->print("ADS-B Radar");
    gfx->setTextSize(1);
    
    // Handshake: Check for touch to force provisioning mode
    gfx->setTextColor(0x0350, C_BG); // Dim green
    gfx->setCursor(60, 200); gfx->print("TOUCH TO SETUP...");
    
    unsigned long start_wait = millis();
    bool force_ap = false;
    while (millis() - start_wait < 2500) {
        if (read_touch()) {
            force_ap = true;
            break;
        }
        delay(20);
    }

    // Load Settings
    bool has_settings = SettingsManager::load(settings);
    
    if (!has_settings || force_ap) {
        gfx->fillRect(0, 180, 480, 60, C_BG);
        gfx->setCursor(60, 180); 
        gfx->print(force_ap ? "FORCING SETUP..." : "NO CONFIG! AP MODE...");
        ProvisioningManager::startPortal(settings);
    }
    
    range_nm = settings.range_nm; // Initialize range from settings

    gfx->setCursor(60, 180); gfx->print("Connecting WiFi...");
    Serial.printf("Connecting to %s...\n", settings.wifi_ssid);

    WiFi.begin(settings.wifi_ssid, settings.wifi_password);
    WiFi.setSleep(false);
    
    unsigned long start_wifi = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start_wifi < 10000) {
        delay(500);
    }

    if (WiFi.status() != WL_CONNECTED) {
        gfx->fillRect(60, 180, 400, 20, C_BG);
        gfx->setCursor(60, 180); gfx->print("WiFi failed! AP Mode...");
        ProvisioningManager::startPortal(settings);
    }

    Serial0.printf("WiFi: %s\n", WiFi.localIP().toString().c_str());
    ArduinoOTA.setHostname("esp32-radar");
    ArduinoOTA.begin();

    // Initialize NTP
    Serial.printf("Syncing NTP (GMT Offset: %.1f)...\n", settings.gmt_offset);
    configTime(settings.gmt_offset * 3600, 0, "pool.ntp.org", "time.nist.gov");

    ac_mutex = xSemaphoreCreateMutex();

    // Fetch task on Core 0 — main loop (sweep/render) runs on Core 1
    xTaskCreatePinnedToCore(fetch_task, "fetch", 8192, nullptr, 1, nullptr, 0);

    // Initial data fetch (blocking, before render starts)
    fetch_requested = true;
    while (fetch_busy || fetch_requested) delay(50);

    full_redraw();
    notify_pi_settings();             // Tell Pi location + timezone on every boot
    notify_pi_app_mode(current_app);  // Tell Pi the initial screen on boot
    Serial0.println(String("VOL:") + String(settings.volume));  // Sync volume on boot

    // Prepare assistant canvas
    assistant_canvas = new Arduino_Canvas(SCREEN_WIDTH, SCREEN_HEIGHT, gfx, 0, 0, 0);
    if (!assistant_canvas->begin(GFX_SKIP_OUTPUT_BEGIN)) {
        Serial.println("Warning: Assistant canvas failed to initialize");
        delete assistant_canvas;
        assistant_canvas = nullptr;
    }
    AssistantView::set_canvas(assistant_canvas);
    AssistantView::set_gfx(gfx);
    AssistantView::set_vsync_sem(vsync_sem);
    AssistantView::init();

    // Settings view shares the same canvas (never active simultaneously)
    SettingsView::set_canvas(assistant_canvas);
    SettingsView::set_gfx(gfx);
    SettingsView::set_vsync_sem(vsync_sem);
    SettingsView::init(settings.volume);

    // Initialize LVGL



    lv_init();
    // Full-screen PSRAM double buffers. Must use MALLOC_CAP_SPIRAM — internal SRAM
    // is exhausted by the 115KB DMA bounce buffer and FreeRTOS stacks.
    size_t buf_size = SCREEN_WIDTH * SCREEN_HEIGHT;
    lv_color_t *buf1 = (lv_color_t *)heap_caps_malloc(buf_size * sizeof(lv_color_t), MALLOC_CAP_SPIRAM);
    lv_color_t *buf2 = (lv_color_t *)heap_caps_malloc(buf_size * sizeof(lv_color_t), MALLOC_CAP_SPIRAM);
    if (!buf1 || !buf2) {
        Serial.println("LVGL PSRAM buffer alloc failed, falling back to 40-row SRAM");
        buf_size = SCREEN_WIDTH * 40;
        buf1 = (lv_color_t *)malloc(buf_size * sizeof(lv_color_t));
        buf2 = NULL;
    }
    lv_disp_draw_buf_init(&draw_buf, buf1, buf2, buf_size);

    lv_disp_drv_init(&disp_drv);
    disp_drv.hor_res = SCREEN_WIDTH;
    disp_drv.ver_res = SCREEN_HEIGHT;
    disp_drv.flush_cb = my_disp_flush;
    disp_drv.draw_buf = &draw_buf;
    disp_drv.full_refresh = 1;      // Re-enabled: Fix tearing issue with ESP32 DMA
    disp_drv.antialiasing = 1;
    lv_disp_drv_register(&disp_drv);

    static lv_indev_drv_t indev_drv;
    lv_indev_drv_init(&indev_drv);
    indev_drv.type = LV_INDEV_TYPE_POINTER;
    indev_drv.read_cb = my_touchpad_read;
    lv_indev_drv_register(&indev_drv);

    ClockView::init();
    GlobeView::init();

    notify_pi_app_mode(current_app);
}

void loop() {
    uint32_t now = millis();
    
    static uint32_t last_wifi_print = 0;
    if (now - last_wifi_print > 10000) {
        last_wifi_print = now;
        Serial0.printf("WIFI_STATUS:%d,RSSI:%d,IP:%s\n", (int)WiFi.status(), (int)WiFi.RSSI(), WiFi.localIP().toString().c_str());
    }

    // Touch processing
    touch_active = read_touch();
    bool is_new_tap = touch_active && !last_was_touching;

    static bool long_press_fired  = false;
    static bool gesture_consumed  = false;
    static bool arc_dragging      = false;
    static bool touch_has_moved   = false;
    #define LONG_PRESS_MS 800

    if (is_new_tap) {
        touch_start_x    = touch_x;
        touch_start_y    = touch_y;
        last_touch_time  = now;
        long_press_fired = false;
        gesture_consumed = false;
        touch_has_moved  = false;

        // Arc drag: starts if finger lands on the arc ring zone
        if (current_app == APP_SETTINGS) {
            float dist = sqrtf((float)(touch_x - CX) * (touch_x - CX) +
                               (float)(touch_y - CY) * (touch_y - CY));
            arc_dragging = (dist >= 160.0f && dist <= 240.0f);
            if (arc_dragging) {
                gesture_consumed = true;
            } else {
                // Not dragging the arc? Check for Diagnostic Button (Instant Switch)
                // Expanded hit box: Y > 240 (bottom half) and not near edge
                if (touch_y > 240 && touch_x > 100 && touch_x < 380) {
                    current_app = APP_DIAGNOSTICS;
                    DiagnosticsView::init();
                    Serial.println("App State: DIAGNOSTICS (Instant)");
                    gesture_consumed = true;
                }
            }
        } else {
            arc_dragging = false;
        }
    }

    // ── Track movement to prevent accidental long-presses ─────────────────────
    if (touch_active && !touch_has_moved) {
        int dx = abs(touch_x - touch_start_x);
        int dy = abs(touch_y - touch_start_y);
        if (dx + dy > TAP_THRESHOLD) touch_has_moved = true;
    }

    // ── Live arc drag: update volume as finger moves along the ring ───────────
    if (touch_active && arc_dragging) {
        static unsigned long last_drag_ms = 0;
        int new_vol = SettingsView::touch_to_volume(touch_x, touch_y);
        if (new_vol != SettingsView::get_volume() && (now - last_drag_ms) >= 33) {
            SettingsView::set_volume(new_vol);
            SettingsView::update();
            last_drag_ms = now;
        }
    }
    
    if (!touch_active && arc_dragging) {
        // Finger lifted — persist the current value (live drag already kept display current).
        // Deliberately NOT calling update() here: any redraw on the lift frame causes a
        // visible flash because the touch event and DMA flush race each other.
        settings.volume = SettingsView::get_volume();
        SettingsManager::save(settings);
        String vmsg = String("VOL:") + String(settings.volume);
        Serial0.println(vmsg);
        Serial.printf("[UART→Pi] %s\n", vmsg.c_str());
        arc_dragging = false;
    }

    static int prev_touch_y = 0;
    // ── Live globe drag: update globe tilt ───────────
    if (touch_active && current_app == APP_GLOBE && !is_new_tap) {
        float delta_tilt = (touch_y - prev_touch_y) * 0.008f;
        GlobeView::add_tilt(delta_tilt);
    }

    // ── Long-press detection (800ms still hold — only outside arc zone) ───────
    if (touch_active && !long_press_fired && !arc_dragging && !touch_has_moved) {
        int hold_dx = abs(touch_x - touch_start_x);
        int hold_dy = abs(touch_y - touch_start_y);
        if ((hold_dx + hold_dy) < TAP_THRESHOLD && (now - last_touch_time) >= LONG_PRESS_MS) {
            long_press_fired = true;
            gesture_consumed = true;
            if (current_app != APP_SETTINGS) enter_settings();
            else                              exit_settings();
        }
    }

    // ── On release: process gesture (skipped if long-press or arc-drag consumed) ─
    if (!touch_active && last_was_touching && !gesture_consumed) {
        int dx = abs(touch_x - touch_start_x);
        int dy = abs(touch_y - touch_start_y);
        bool is_tap = (dx + dy) < TAP_THRESHOLD;

        // Tap while assistant is speaking = interrupt
        if (is_tap && AssistantView::get_state() == AssistantView::STATE_SPEAKING) {
            AssistantView::set_state(AssistantView::STATE_IDLE);  // immediate visual feedback
            Serial0.println("INTERRUPT");
            Serial.println("[UART→Pi] INTERRUPT");
        } else {
            process_swipe(touch_start_x, touch_start_y, touch_x, touch_y);
        }
    }

    last_was_touching = touch_active;
    if (touch_active) prev_touch_y = touch_y;

    // ─── Serial0 Heartbeat (troubleshooting only) ─────────────────────────────
    static unsigned long last_hb_ms = 0;
    if (now - last_hb_ms >= 5000) {
        Serial0.println("HB:OK");
        Serial.println("[UART→Pi] HB:OK");
        last_hb_ms = now;
    }

    if (now - last_pi_msg_ms > 15000) {
        pi_connected = false;
    }
    // ─────────────────────────────────────────────────────────────────────────

    // ── Timer tick — runs on every screen ──────────────────────────────────────
    AssistantView::tick_timer();

    // ── Timer done — notify Pi regardless of active screen ─────────────────────
    if (AssistantView::is_timer_done()) {
        String msg = "TIMER:DONE:" + AssistantView::timer_label();
        Serial0.println(msg);
        Serial.println(msg);
        AssistantView::clear_timer();
        if (current_app == APP_CLOCK) ClockView::set_timer_pct(-1);
        if (current_app == APP_RADAR) full_redraw();  // redraw to erase ring
    }

    if (current_app == APP_RADAR) {
        // Don't run lv_timer_handler in radar mode -- it would flush LVGL's white screen over us

        // Sweep — smooth single-arm rotation
        static unsigned long last_sweep_ms = 0;
        if (now - last_sweep_ms >= SWEEP_INTERVAL_MS) {
            float new_angle = fmodf(sweep_angle + SWEEP_STEP_DEG, 360.0f);
            if (prev_sweep >= 0) erase_sweep(prev_sweep);
            sweep_paint_aircraft(sweep_angle, new_angle);
            draw_sweep(new_angle);
            draw_radar_timer_ring();  // redraws ring if active (persists outside sweep radius)
            if (detail_visible && detail_clobbered) draw_detail_box();
            prev_sweep  = sweep_angle;
            sweep_angle = new_angle;
            last_sweep_ms = now;
        }

        // Trigger background fetch every FETCH_INTERVAL_MS
        static unsigned long last_fetch_ms = 0;
        if (now - last_fetch_ms >= FETCH_INTERVAL_MS && !fetch_busy) {
            fetch_requested = true;
            last_fetch_ms = now;
        }
    } else if (current_app == APP_ASSISTANT) {
        AssistantView::update();
    } else if (current_app == APP_CLOCK) {
        if (!touch_active) {
            // Update timer arc at ~10 Hz — fast enough to look smooth, won't flood LVGL
            static unsigned long last_timer_arc_ms = 0;
            if (now - last_timer_arc_ms >= 100) {
                if (AssistantView::is_timer_active()) {
                    ClockView::set_timer_pct((int)AssistantView::get_timer_vis_pct());
                }
                last_timer_arc_ms = now;
            }
            ClockView::update_time();
            // VSYNC waiting is now handled inside my_disp_flush to ensure it happens
            // immediately before the DMA transfer, not before the rendering.
            lv_timer_handler();
        }
    } else if (current_app == APP_GLOBE) {
        if (!touch_active) GlobeView::update();
    } else if (current_app == APP_SETTINGS || current_app == APP_DIAGNOSTICS) {
        if (!touch_active) {
            static uint32_t last_diag_req = 0;
            if (now - last_diag_req > 200) { // 5Hz requests
                Serial0.println("DIAG?");
                last_diag_req = now;
            }
            if (current_app == APP_DIAGNOSTICS) {
                DiagnosticsView::update();
            }
        }
    }

    // ─── UART Remote Control (Pi Brains) ───
    static String rx_buf_usb = "";
    static String rx_buf_uart = "";

    auto handle_cmd = [&](String rx) {
        rx.trim();
        if (rx.length() == 0) return;
        if (!pi_connected) {
            pi_connected = true;
            if (strlen(settings.wifi_ssid) > 0) {
                Serial0.printf("WIFI:%s|%s\n", settings.wifi_ssid, settings.wifi_password);
            }
        }
        last_pi_msg_ms = millis();
        if (rx == "HB:ACK") {
            return;
        } else if (rx.startsWith("SLEEP:")) {
            int mode = rx.substring(6).toInt();
            // Bypassing AssistantView::set_state to prevent task deadlock during animation
            set_display_power(mode == 0); // 1 = Sleep (Off), 0 = Wake (On)
        } else if (rx == "SYNC?") {


            notify_pi_settings();
            notify_pi_app_mode(current_app);
        } else if (rx == "DIAG") {
            current_app = APP_DIAGNOSTICS;
            DiagnosticsView::init();
            Serial.println("Force Switching to DIAGNOSTICS");
        } else if (rx == "Z+") {
            range_nm = max((float)MIN_RANGE_NM, range_nm / 1.15f);
            Serial.printf("Remote Zoom IN: %.1f nm\n", range_nm);
            full_redraw();
        } else if (rx == "Z-") {
            range_nm = min((float)MAX_RANGE_NM, range_nm * 1.15f);
            Serial.printf("Remote Zoom OUT: %.1f nm\n", range_nm);
            full_redraw();
        } else if (rx.startsWith("Z:")) {
            float val = rx.substring(2).toFloat();
            if (val >= MIN_RANGE_NM && val <= MAX_RANGE_NM) {
                range_nm = val;
                Serial.printf("Remote Zoom SET: %.1f nm\n", range_nm);
                full_redraw();
            }
        } else if (rx.startsWith("TIMER:START:")) {
            String rest = rx.substring(12);
            int colon = rest.indexOf(':');
            uint32_t secs;
            String lbl;
            if (colon != -1) {
                secs = (uint32_t)rest.substring(0, colon).toInt();
                lbl  = rest.substring(colon + 1);
            } else {
                secs = (uint32_t)rest.toInt();
                lbl  = "";
            }
            AssistantView::start_timer(secs, lbl);
        } else if (rx == "STYLE:TOGGLE") {
            AssistantView::toggle_style();
            Serial.println("Remote Style Toggle");
        } else if (rx == "REBOOT" || rx == "!") {
            Serial.println("Rebooting via UART command...");
            delay(500);
            ESP.restart();
        } else if (rx == "TIMER:CANCEL") {
            AssistantView::clear_timer();
        } else if (rx.startsWith("WAKE|")) {
            if (current_app != APP_ASSISTANT) {
                if (current_app == APP_CLOCK) {
                    lvgl_active = false;
                    lv_obj_t *blank = lv_obj_create(NULL);
                    lv_obj_set_style_bg_color(blank, lv_color_black(), 0);
                    lv_scr_load(blank);
                }
                current_app = APP_ASSISTANT;
                AssistantView::show();
                notify_pi_app_mode(current_app);
            }
            AssistantView::set_state(AssistantView::STATE_LISTENING);
        } else if (rx.startsWith("APP:")) {
            String cmd = rx.substring(4);
            cmd.trim();
            if (cmd == "THINKING") AssistantView::set_state(AssistantView::STATE_THINKING);
            else if (cmd == "SPEAKING") AssistantView::set_state(AssistantView::STATE_SPEAKING);
            else if (cmd == "CONTINUITY") AssistantView::set_state(AssistantView::STATE_CONTINUITY);
            else if (cmd == "ASSISTANT") AssistantView::set_state(AssistantView::STATE_IDLE);
        } else if (rx.startsWith("EMO:")) {
            String emo = rx.substring(4);
            emo.trim();
            if (emo == "NEUTRAL")       AssistantView::set_emotion(AssistantView::EMO_NEUTRAL);
            else if (emo == "HAPPY")    AssistantView::set_emotion(AssistantView::EMO_HAPPY);
            else if (emo == "SARDONIC") AssistantView::set_emotion(AssistantView::EMO_SARDONIC);
            else if (emo == "ALERT")    AssistantView::set_emotion(AssistantView::EMO_ALERT);
            else if (emo == "WINK")     AssistantView::set_emotion(AssistantView::EMO_WINK);
        } else if (rx.startsWith("VMODE:")) {
            String mode = rx.substring(6);
            mode.trim();
            if (mode == "IRIS")      AssistantView::set_style(AssistantView::STYLE_IRIS);
            else if (mode == "FACE") AssistantView::set_style(AssistantView::STYLE_FACE);
        } else if (rx.startsWith("A")) {
            int intensity = rx.substring(1).toInt();
            AssistantView::set_audio_intensity(intensity);
        } else if (rx.startsWith("S")) {
            int pipe_idx = rx.indexOf('|');
            String s_data;
            String a_data = "";
            if (pipe_idx != -1) {
                s_data = rx.substring(1, pipe_idx);
                a_data = rx.substring(pipe_idx + 1);
            } else {
                s_data = rx.substring(1);
            }
            int bins[16];
            int bin_idx = 0;
            int start_idx = 0;
            while (bin_idx < 16 && start_idx < s_data.length()) {
                int next_comma = s_data.indexOf(',', start_idx);
                if (next_comma == -1) {
                    bins[bin_idx++] = s_data.substring(start_idx).toInt();
                    break;
                }
                bins[bin_idx++] = s_data.substring(start_idx, next_comma).toInt();
                start_idx = next_comma + 1;
            }
            AssistantView::set_spectrum(bins, bin_idx);
            if (a_data.startsWith("A")) {
                int intensity = a_data.substring(1).toInt();
                AssistantView::set_audio_intensity(intensity);
                DiagnosticsView::set_mic_intensity(intensity);
            }
        } else if (rx.startsWith("DIAG:")) {
            String data = rx.substring(5);
            int start = 0;
            while (start < data.length()) {
                int comma = data.indexOf(',', start);
                String kv = (comma == -1) ? data.substring(start) : data.substring(start, comma);
                int eq = kv.indexOf('=');
                if (eq != -1) {
                    String k = kv.substring(0, eq);
                    String v = kv.substring(eq + 1);
                    if (k == "mic") DiagnosticsView::set_mic_intensity(v.toInt());
                    else if (k == "ww") DiagnosticsView::set_oww_status(v.c_str());
                    else if (k == "pi") DiagnosticsView::set_pi_status(v.c_str());
                    else if (k == "rssi") {
                        DiagnosticsView::set_wifi_rssi(v.toInt());
                        SettingsView::set_pi_rssi(v.toInt());
                    }
                    else if (k == "wake") DiagnosticsView::set_last_wake(v.c_str());
                }
                if (comma == -1) break;
                start = comma + 1;
            }
        }
    };

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') { handle_cmd(rx_buf_usb); rx_buf_usb = ""; }
        else if (c != '\r') rx_buf_usb += c;
    }
    while (Serial0.available()) {
        char c = Serial0.read();
        if (c == '\n' || c == '!') { // Support '!' as a hard-terminator for emergency reboots
            handle_cmd(rx_buf_uart); rx_buf_uart = ""; 
        }
        else if (c != '\r') rx_buf_uart += c;
    }

    
    ArduinoOTA.handle();

    delay(5);
}
