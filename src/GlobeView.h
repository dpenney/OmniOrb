#pragma once
#include <stdint.h>
#include <vector>

struct GlobePOI {
    float lat, lon;
    uint16_t color;
    int old_x = -1, old_y = -1;
};

class GlobeView {
public:
    static void init();
    static void show();
    static void update();
    static void add_tilt(float dt);
    static void toggle_rotation();
    
    static void clear_pois();
    static void add_poi(float lat, float lon, uint16_t color);

private:
    static std::vector<GlobePOI> pois;
};
