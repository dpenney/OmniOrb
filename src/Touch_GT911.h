#pragma once

#include <Arduino.h>
#include <Wire.h>

#define GT911_ADDR 0x5D // or 0x14 depending on reset pin state, but demo uses 0x5D

// GT911 Register Map
#define GT911_PRODUCT_ID_REG  0x8140
#define GT911_READ_XY_REG     0x814E
#define GT911_CONFIG_REG      0x8047

struct TouchPoint {
    uint16_t x;
    uint16_t y;
    uint16_t size;
};

class Touch_GT911 {
public:
    Touch_GT911();
    
    // Initialize the GT911. Call this AFTER the TCA9554 resets it.
    bool begin();
    
    // Read the touch status. Returns true if there is at least one active touch
    bool read();
    
    // The number of current touch points (0-5)
    uint8_t points;
    
    // Array of active touch coordinates
    TouchPoint touches[5];

private:
    uint8_t _address;

    bool readRegisterData(uint16_t reg, uint8_t *data, size_t length);
    bool writeRegisterData(uint16_t reg, const uint8_t *data, size_t length);
    bool writeRegister8(uint16_t reg, uint8_t data);
};
