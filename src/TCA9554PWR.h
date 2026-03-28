#pragma once

#include <Arduino.h>
#include <Wire.h>
#include "pins.h"

class TCA9554PWR {
public:
    TCA9554PWR(uint8_t address = TCA9554_ADDR);
    
    void begin();
    
    // Read the value of a single pin (1-8)
    uint8_t readPin(uint8_t pin);
    
    // Read all pins (returns 8-bit state)
    uint8_t readAll();
    
    // Set pin mode (0 = Output, 1 = Input)
    void pinMode(uint8_t pin, uint8_t state);
    void modeAll(uint8_t pinState); // 0 = output, 1 = input for each bit
    
    // Set pin output level (0 = Low, 1 = High)
    void digitalWrite(uint8_t pin, uint8_t state);
    void writeAll(uint8_t pinState);

private:
    uint8_t _address;
    
    // Registers
    static const uint8_t REG_INPUT  = 0x00;
    static const uint8_t REG_OUTPUT = 0x01;
    static const uint8_t REG_POLAR  = 0x02;
    static const uint8_t REG_CONFIG = 0x03;

    uint8_t readRegister(uint8_t reg);
    bool writeRegister(uint8_t reg, uint8_t data);
};
