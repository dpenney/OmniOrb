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
// Allocated from PSRAM in init() to avoid starving the display DMA bounce
// buffer, which requires ~115KB of contiguous internal SRAM.
static Point3D base_nodes[MAX_GLOBE_NODES];
static int old_x[MAX_GLOBE_NODES], old_y[MAX_GLOBE_NODES];
static int num_nodes = 0;

#include "globe_map.h"

// Store old home marker position to erase it cleanly
static int old_home_x = -1, old_home_y = -1;
static int old_bark_x = -1, old_bark_y = -1;

const float BARK_LAT = 33.771524f;
const float BARK_LON = -92.858774f;

static float rot_y = 0.0f;
static float tilt_x = -0.41f; // Current globe tilt
static unsigned long last_frame_time = 0;

void GlobeView::add_tilt(float dt) {
    tilt_x += dt;
    if (tilt_x > M_PI / 2.0f) tilt_x = M_PI / 2.0f;
    if (tilt_x < -M_PI / 2.0f) tilt_x = -M_PI / 2.0f;
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
    old_home_x = -1; old_home_y = -1;
    old_bark_x = -1; old_bark_y = -1;
    last_frame_time = millis();
}

void GlobeView::update() {
    unsigned long now = millis();
    if (now - last_frame_time < 33) return; // ~30 fps cap
    last_frame_time = now;

    // Spin speed
    rot_y += 0.012f;
    if (rot_y > M_PI * 2) rot_y -= M_PI * 2;

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
    if (old_home_x >= 0) {
        gfx->fillCircle(old_home_x, old_home_y, 3, C_BG);
        gfx->drawFastHLine(old_home_x - 5, old_home_y, 11, C_BG);
        gfx->drawFastVLine(old_home_x, old_home_y - 5, 11, C_BG);
    }
    if (old_bark_x >= 0) {
        gfx->fillCircle(old_bark_x, old_bark_y, 3, C_BG);
    }
    old_home_x = -1;
    old_bark_x = -1;

    // 3. Process and draw current home marker
    float r_lat = settings.home_lat * M_PI / 180.0f;
    float r_lon = settings.home_lon * M_PI / 180.0f;
    
    // Spherical to Cartesian (matches continent map logic)
    float ax = cosf(r_lat) * sinf(r_lon);
    float ay = sinf(r_lat);
    float az = cosf(r_lat) * cosf(r_lon);

    // Apply Y spin rotation
    float rx = ax * cos_y - az * sin_y;
    float rz = ax * sin_y + az * cos_y;
    float ry = ay;
    
    // Apply X tilt rotation
    float fz = rz * cos_x - ry * sin_x;
    float fy = rz * sin_x + ry * cos_x;

    if (fz >= 0) {
        int sx = CX + (int)(rx * RADIUS);
        int sy = CY - (int)(fy * RADIUS);
        
        // Draw home marker (Cross + Circle for prominence)
        gfx->fillCircle(sx, sy, 3, C_HOME);
        gfx->drawFastHLine(sx - 5, sy, 11, C_HOME);
        gfx->drawFastVLine(sx, sy - 5, 11, C_HOME);
        
        old_home_x = sx;
        old_home_y = sy;
    }

    // 4. Process and draw Barksdale AFB (Blue Dot)
    float b_lat = BARK_LAT * M_PI / 180.0f;
    float b_lon = BARK_LON * M_PI / 180.0f;
    float bax = cosf(b_lat) * sinf(b_lon);
    float bay = sinf(b_lat);
    float baz = cosf(b_lat) * cosf(b_lon);

    float brx = bax * cos_y - baz * sin_y;
    float brz = bax * sin_y + baz * cos_y;
    float bry = bay;

    float bfz = brz * cos_x - bry * sin_x;
    float bfy = brz * sin_x + bry * cos_x;

    if (bfz >= 0) {
        int sx = CX + (int)(brx * RADIUS);
        int sy = CY - (int)(bfy * RADIUS);
        gfx->fillCircle(sx, sy, 3, C_POI);
        old_bark_x = sx;
        old_bark_y = sy;
    }
    
    // Draw crosshair at the center
    gfx->drawFastHLine(CX - 15, CY, 30, C_DIM_DOT);
    gfx->drawFastVLine(CX, CY - 15, 30, C_DIM_DOT);
}
