/**
 * @file pins.h
 * @brief GPIO pin assignments for the Waveshare ESP32-S3-Knob-Touch-LCD-1.8
 *
 * Board: https://www.waveshare.com/esp32-s3-knob-touch-lcd-1.8.htm
 */

#pragma once

// ── Display (ST7701S RGB) ─────────────────────────────────────────────────────
#define LCD_CLK_PIN    2
#define LCD_MOSI_PIN   1 
#define LCD_BL         6   ///< Backlight enable (PWM or HIGH = on)

// RGB Interface Pins
#define LCD_RGB_HSYNC  38
#define LCD_RGB_VSYNC  39
#define LCD_RGB_DE     40
#define LCD_RGB_PCLK   41
#define LCD_RGB_DISP   -1

// RGB Data Pins (16-bit)
#define LCD_RGB_D0     5
#define LCD_RGB_D1     45
#define LCD_RGB_D2     48
#define LCD_RGB_D3     47
#define LCD_RGB_D4     21
#define LCD_RGB_D5     14
#define LCD_RGB_D6     13
#define LCD_RGB_D7     12
#define LCD_RGB_D8     11
#define LCD_RGB_D9     10
#define LCD_RGB_D10    9
#define LCD_RGB_D11    46
#define LCD_RGB_D12    3
#define LCD_RGB_D13    8
#define LCD_RGB_D14    18
#define LCD_RGB_D15    17

// ── Touch controller (GT911) & IO Expander (TCA9554) ────────────────────────
// The shared I2C bus is used for the Touch Controller AND the TCA9554
#define I2C_SDA        15
#define I2C_SCL        7

#define TOUCH_INT      16   ///< GT911 Interrupt
#define TCA9554_ADDR   0x20

// TCA9554 IO Expander Pins
#define EXIO_PIN1      1   ///< ST7701 Reset
#define EXIO_PIN2      2   ///< GT911 Reset
#define EXIO_PIN3      3   ///< ST7701 CS
#define EXIO_PIN4      4
#define EXIO_PIN5      5
#define EXIO_PIN6      6
#define EXIO_PIN7      7
#define EXIO_PIN8      8

// ── Encoder (Commented out — no encoder on 2.8C board) ──────────────────────
// #define ENCODER_A   8
// #define ENCODER_B   7
