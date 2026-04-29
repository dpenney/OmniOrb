#pragma once

class GlobeView {
public:
    static void init();
    static void show();
    static void update();
    static void add_tilt(float dt);
};
