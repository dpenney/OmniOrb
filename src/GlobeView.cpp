#include "GlobeView.h"
#include <Arduino_GFX_Library.h>
#include <esp_heap_caps.h>
#include <math.h>
#include "config.h"
#include "Settings.h"
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

extern Arduino_RGB_Display *gfx;
extern ProjectSettings settings;

// Remove the ADS-B arrays as we only show the home location now.
// extern Aircraft aircraft[];
// extern int aircraft_count;
// extern SemaphoreHandle_t ac_mutex;

std::vector<GlobePOI> GlobeView::pois;

#define MAX_GLOBE_NODES 2800
#define CX              240
#define CY              240
#define RADIUS          220

#define C_BG            0x0000
#define C_DIM_DOT       0x0520 // Medium green for backface land (brightened to avoid LCD crushing)
#define C_BRIGHT_DOT    0xc7e0 // Bright neon cyan for frontface land
#define C_AIRCRAFT      0xFFFF // White for aircraft
#define C_HOME          0xF800 // Red for home marker
#define C_POI           0x041F // Bright Blue for special locations

struct Point3D { float x, y, z; };
// Static BSS arrays (~56KB internal SRAM total). If internal RAM gets tight,
// move these to heap_caps_malloc(MALLOC_CAP_SPIRAM) in init().
static Point3D base_nodes[MAX_GLOBE_NODES];
static int old_x[MAX_GLOBE_NODES], old_y[MAX_GLOBE_NODES];
static int num_nodes = 0;

#include "globe_map.h"

// POI Management
void GlobeView::clear_pois() {
    pois.clear();
}

void GlobeView::add_poi(float lat, float lon, uint16_t color) {
    pois.push_back({lat, lon, color, -1, -1});
}

static float rot_y = 0.0f;
static float tilt_x = -0.41f; // Current globe tilt
static bool is_rotating = true;
static unsigned long last_frame_time = 0;

void GlobeView::add_tilt(float dt) {
    tilt_x += dt;
    if (tilt_x > M_PI / 2.0f) tilt_x = M_PI / 2.0f;
    if (tilt_x < -M_PI / 2.0f) tilt_x = -M_PI / 2.0f;
}

void GlobeView::toggle_rotation() {
    is_rotating = !is_rotating;
    Serial.printf("[DEBUG] Globe Rotation: %s\n", is_rotating ? "ON" : "OFF");
}

void GlobeView::init() {
    float phi = M_PI * (3.0f - sqrtf(5.0f));
    num_nodes = 0;
    
    int max_candidates = 7500; // Total evaluation pool. Set lower than 10000 so we evaluate the entire sphere down to the South Pole before hitting the 2800 node limit.
    for (int i = 0; i < max_candidates; i++) {
        if (num_nodes >= MAX_GLOBE_NODES) break;
        
        float y = 1.0f - (i / (float)(max_candidates - 1)) * 2.0f;
        float r = sqrtf(1.0f - y * y);
        float theta = phi * i;
        float x = cosf(theta) * r;
        float z = sinf(theta) * r;
        
        // Convert to spherical
        float lat = asinf(y) * 180.0f / M_PI;
        float lon = atan2f(x, z) * 180.0f / M_PI;
        
        int py = (int)((90.0f - lat) / 180.0f * 63.99f);
        // Correcting longitude: atan2f returns -180 to 180.
        // We add 180 to make it 0 to 360, then map it to 0-127.
        int px = (int)((lon + 180.0f) / 360.0f * 127.99f);
        
        if (py >= 0 && py < 64 && px >= 0 && px < 128) {
            int byte_idx = px / 8;
            int bit_idx = 7 - (px % 8);
            if (world_map_128x64[py][byte_idx] & (1 << bit_idx)) {
                base_nodes[num_nodes] = {x, y, z};
                old_x[num_nodes] = -1;
                old_y[num_nodes] = -1;
                num_nodes++;
            }
        }
    }
}

void GlobeView::show() {
    gfx->fillScreen(C_BG);
    for(int i = 0; i < num_nodes; i++) {
        old_x[i] = -1; old_y[i] = -1;
    }
    for (auto &poi : pois) {
        poi.old_x = -1;
        poi.old_y = -1;
    }
    last_frame_time = millis();
}

void GlobeView::update() {
    unsigned long now = millis();
    if (now - last_frame_time < 33) return; // ~30 fps cap
    last_frame_time = now;

    // Spin speed
    if (is_rotating) {
        rot_y += 0.012f;
        if (rot_y > M_PI * 2) rot_y -= M_PI * 2;
    }

    float cos_y = cosf(rot_y);
    float sin_y = sinf(rot_y);
    
    // Use dynamic tilt_x for X axis rotation
    float cos_x  = cosf(tilt_x);
    float sin_x  = sinf(tilt_x);

    // 1. Process and draw globe nodes (Continents)
    for (int i = 0; i < num_nodes; i++) {
        // Erase old
        if (old_x[i] >= 0) {
            gfx->fillRect(old_x[i], old_y[i], 2, 2, C_BG);
        }

        // Spin around Y axis
        float px = base_nodes[i].x * cos_y - base_nodes[i].z * sin_y;
        float pz = base_nodes[i].x * sin_y + base_nodes[i].z * cos_y;
        float py = base_nodes[i].y;
        
        // Tilt down on X axis
        float final_z = pz * cos_x - py * sin_x;
        float final_y = pz * sin_x + py * cos_x;

        int sx = CX + (int)(px * RADIUS);
        int sy = CY - (int)(final_y * RADIUS);

        old_x[i] = sx;
        old_y[i] = sy;

        uint16_t color = (final_z >= 0) ? C_BRIGHT_DOT : C_DIM_DOT;
        if (final_z >= 0) {
            gfx->fillRect(sx, sy, 2, 2, color);
        } else {
            gfx->drawPixel(sx, sy, color);
        }
    }

    // 2. Erase old markers
    for (auto &poi : pois) {
        if (poi.old_x >= 0) {
            gfx->fillCircle(poi.old_x, poi.old_y, 3, C_BG);
            poi.old_x = -1;
        }
    }

    // 3. Process and draw Dynamic POIs
    for (auto &poi : pois) {
        float p_lat = poi.lat * M_PI / 180.0f;
        float p_lon = poi.lon * M_PI / 180.0f;
        float pax = cosf(p_lat) * sinf(p_lon);
        float pay = sinf(p_lat);
        float paz = cosf(p_lat) * cosf(p_lon);

        float prx = pax * cos_y - paz * sin_y;
        float prz = pax * sin_y + paz * cos_y;
        float pry = pay;

        float pfz = prz * cos_x - pry * sin_x;
        float pfy = prz * sin_x + pry * cos_x;

        if (pfz >= 0) {
            int sx = CX + (int)(prx * RADIUS);
            int sy = CY - (int)(pfy * RADIUS);
            gfx->fillCircle(sx, sy, 3, poi.color);
            poi.old_x = sx;
            poi.old_y = sy;
        }
    }
    
    // Draw crosshair at the center
    gfx->drawFastHLine(CX - 15, CY, 30, C_DIM_DOT);
    gfx->drawFastVLine(CX, CY - 15, 30, C_DIM_DOT);
}
