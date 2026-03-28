#pragma once

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include "pins.h"
#include "TCA9554PWR.h"

extern TCA9554PWR io_expander;
extern Arduino_RGB_Display *gfx;

Arduino_RGB_Display *create_waveshare_28C_rgb_panel();