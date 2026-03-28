/**
 * @file TCA9554PWR.cpp
 * @brief Implementation of the TCA9554 I2C I/O expander driver.
 *
 * This class provides a simple Arduino-like interface (pinMode, digitalWrite, readPin)
 * for the 8-bit TCA9554 I/O expander used on the Waveshare board. It handles I2C
 * register reading and writing to control hardware resets and other peripheral states.
 */
#include "TCA9554PWR.h"

// Constructor: Stores the I2C address of the expander
TCA9554PWR::TCA9554PWR(uint8_t address) : _address(address) {
}

// Initialize the I2C connection to the device
void TCA9554PWR::begin() {
    Wire.beginTransmission(_address);
    Wire.endTransmission();
}

// Reads a single byte from the specified TCA9554 register
uint8_t TCA9554PWR::readRegister(uint8_t reg) {
    Wire.beginTransmission(_address);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) {
        Serial.println("TCA9554 I2C read failed!");
        return 0;
    }
    Wire.requestFrom(_address, (uint8_t)1);
    return Wire.read();
}

// Writes a single byte of data to the specified TCA9554 register
bool TCA9554PWR::writeRegister(uint8_t reg, uint8_t data) {
    Wire.beginTransmission(_address);
    Wire.write(reg);
    Wire.write(data);
    return (Wire.endTransmission() == 0);
}

// Sets the mode of a specific expander pin (1-8). 
// state: 1 = Input (high impedance), 0 = Output
void TCA9554PWR::pinMode(uint8_t pin, uint8_t state) {
    if (pin < 1 || pin > 8) return;
    uint8_t current = readRegister(REG_CONFIG);
    uint8_t mask = (1 << (pin - 1));
    if (state == 1) {
        current |= mask; // Input
    } else {
        current &= ~mask; // Output
    }
    writeRegister(REG_CONFIG, current);
}

// Sets the exact mode for all 8 pins simultaneously using a bitmask
void TCA9554PWR::modeAll(uint8_t pinState) {
    writeRegister(REG_CONFIG, pinState);
}

uint8_t TCA9554PWR::readPin(uint8_t pin) {
    if (pin < 1 || pin > 8) return 0;
    uint8_t inputs = readRegister(REG_INPUT);
    return (inputs >> (pin - 1)) & 0x01;
}

// Reads the state of all 8 pins simultaneously
uint8_t TCA9554PWR::readAll() {
    return readRegister(REG_INPUT);
}

// Sets the output high (1) or low (0) for a specific pin (1-8)
void TCA9554PWR::digitalWrite(uint8_t pin, uint8_t state) {
    if (pin < 1 || pin > 8) return;
    uint8_t current = readRegister(REG_OUTPUT);
    uint8_t mask = (1 << (pin - 1));
    if (state == 1) {
        current |= mask;
    } else {
        current &= ~mask;
    }
    writeRegister(REG_OUTPUT, current);
}

// Directly writes an 8-bit mask to set the entire output register
void TCA9554PWR::writeAll(uint8_t pinState) {
    writeRegister(REG_OUTPUT, pinState);
}
