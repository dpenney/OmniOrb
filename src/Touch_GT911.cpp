/**
 * @file Touch_GT911.cpp
 * @brief Implementation of the GT911 capacitive touch panel driver.
 *
 * Provides a minimal, custom I2C interface to communicate with the Goodix GT911
 * touch controller. Handles reading X/Y coordinates, touch size, and clearing
 * hardware interrupt flags for proper continuous touch operation.
 */
#include "Touch_GT911.h"

// Constructor: Initializes internal points counter and sets the I2C address
Touch_GT911::Touch_GT911() : points(0), _address(GT911_ADDR) {
    memset(touches, 0, sizeof(touches));
}

// Verifies communication with the GT911 by reading its Product ID
bool Touch_GT911::begin() {
    // Read product ID to verify communication
    uint8_t buf[4] = {0};
    if (readRegisterData(GT911_PRODUCT_ID_REG, buf, 3)) {
        Serial.printf("GT911 ID: %c%c%c\n", buf[0], buf[1], buf[2]);
        return true;
    }
    Serial.println("GT911 Touch not found!");
    return false;
}

// Polls the GT911 over I2C to see if new touch data is available. 
// If available, parses up to 5 touch points into the `touches` array.
bool Touch_GT911::read() {
    uint8_t status;
    points = 0;
    
    if (!readRegisterData(GT911_READ_XY_REG, &status, 1)) {
        return false;
    }

    // Check if buffer status is ready (bit 7)
    if ((status & 0x80) == 0x00) {
        // Nothing to read or not ready
        return false;
    }

    uint8_t touch_cnt = status & 0x0F;
    if (touch_cnt > 5) touch_cnt = 5;

    if (touch_cnt > 0) {
        uint8_t buf[40]; // 5 touches * 8 bytes each
        if (readRegisterData(GT911_READ_XY_REG + 1, buf, touch_cnt * 8)) {
            points = touch_cnt;
            for (int i = 0; i < touch_cnt; i++) {
                // Each point is 8 bytes: [TrackID, X_Low, X_High, Y_Low, Y_High, Size_Low, Size_High, Reserved]
                touches[i].x = ((uint16_t)buf[(i * 8) + 2] << 8) | buf[(i * 8) + 1];
                touches[i].y = ((uint16_t)buf[(i * 8) + 4] << 8) | buf[(i * 8) + 3];
                touches[i].size = ((uint16_t)buf[(i * 8) + 6] << 8) | buf[(i * 8) + 5];
            }
        }
    }

    // Clear the buffer status bit so GT911 can generate new interrupts/data
    writeRegister8(GT911_READ_XY_REG, 0x00);
    
    return true; // We successfully read a valid status packet
}

// Helper to read multiple consecutive bytes from a 16-bit register address
bool Touch_GT911::readRegisterData(uint16_t reg, uint8_t *data, size_t length) {
    Wire.beginTransmission(_address);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)(reg & 0xFF));
    if (Wire.endTransmission(false) != 0) return false;
    
    Wire.requestFrom((uint8_t)_address, (uint8_t)length);
    for (size_t i = 0; i < length; i++) {
        if (!Wire.available()) return false;
        data[i] = Wire.read();
    }
    return true;
}

// Helper to write multiple consecutive bytes to a 16-bit register address
bool Touch_GT911::writeRegisterData(uint16_t reg, const uint8_t *data, size_t length) {
    Wire.beginTransmission(_address);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)(reg & 0xFF));
    for (size_t i = 0; i < length; i++) {
        Wire.write(data[i]);
    }
    return (Wire.endTransmission() == 0);
}

// Helper to write a single byte to a 16-bit register address
bool Touch_GT911::writeRegister8(uint16_t reg, uint8_t data) {
    return writeRegisterData(reg, &data, 1);
}
