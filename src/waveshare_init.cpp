/**
 * @file waveshare_init.cpp
 * @brief Hardware initialization for the Waveshare ESP32-S3 2.8" RGB Screen.
 *
 * Handles the very specific bit-bang SPI initialization sequence for the ST7701
 * LCD controller, configures the Arduino_GFX RGB panel timings (including the 
 * DMA PCLK tunings and VSnyc settings), and initializes the I2C I/O expander
 * required to un-reset the touch and display peripherals.
 */
#include "waveshare_init.h"
#include <Wire.h>
#include <SPI.h>
#include <driver/spi_master.h>

static spi_device_handle_t SPI_handle = NULL;

void ST7701_WriteCommand(uint8_t cmd) {
    spi_transaction_t spi_tran = {};
    spi_tran.cmd = 0;
    spi_tran.addr = cmd;
    spi_device_transmit(SPI_handle, &spi_tran);
}

void ST7701_WriteData(uint8_t data) {
    spi_transaction_t spi_tran = {};
    spi_tran.cmd = 1;
    spi_tran.addr = data;
    spi_device_transmit(SPI_handle, &spi_tran);
}

void ST7701_Init_Sequence() {
    // 1. Initialize native ESP32 SPI
    spi_bus_config_t buscfg = {};
    buscfg.mosi_io_num = LCD_MOSI_PIN;
    buscfg.miso_io_num = -1;
    buscfg.sclk_io_num = LCD_CLK_PIN;
    buscfg.quadwp_io_num = -1;
    buscfg.quadhd_io_num = -1;
    buscfg.max_transfer_sz = 64;
    spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);

    spi_device_interface_config_t devcfg = {};
    devcfg.command_bits = 1;
    devcfg.address_bits = 8;
    devcfg.mode = 0; // SPI_MODE0
    devcfg.clock_speed_hz = 40000000;
    devcfg.spics_io_num = -1;
    devcfg.queue_size = 1;
    spi_bus_add_device(SPI2_HOST, &devcfg, &SPI_handle);

    // 2. Drive CS LOW
    io_expander.digitalWrite(EXIO_PIN3, LOW);
    delay(10);

    // 3. Send Init Commands
    ST7701_WriteCommand(0xFF); ST7701_WriteData(0x77); ST7701_WriteData(0x01); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x13);
    ST7701_WriteCommand(0xEF); ST7701_WriteData(0x08);
    ST7701_WriteCommand(0xFF); ST7701_WriteData(0x77); ST7701_WriteData(0x01); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x10);
    ST7701_WriteCommand(0xC0); ST7701_WriteData(0x3B); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0xC1); ST7701_WriteData(0x10); ST7701_WriteData(0x0C);
    ST7701_WriteCommand(0xC2); ST7701_WriteData(0x07); ST7701_WriteData(0x0A);
    ST7701_WriteCommand(0xC7); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0xCC); ST7701_WriteData(0x10);
    ST7701_WriteCommand(0xCD); ST7701_WriteData(0x08);
    ST7701_WriteCommand(0xB0); ST7701_WriteData(0x05); ST7701_WriteData(0x12); ST7701_WriteData(0x98); ST7701_WriteData(0x0E); ST7701_WriteData(0x0F); ST7701_WriteData(0x07); ST7701_WriteData(0x07); ST7701_WriteData(0x09); ST7701_WriteData(0x09); ST7701_WriteData(0x23); ST7701_WriteData(0x05); ST7701_WriteData(0x52); ST7701_WriteData(0x0F); ST7701_WriteData(0x67); ST7701_WriteData(0x2C); ST7701_WriteData(0x11);
    ST7701_WriteCommand(0xB1); ST7701_WriteData(0x0B); ST7701_WriteData(0x11); ST7701_WriteData(0x97); ST7701_WriteData(0x0C); ST7701_WriteData(0x12); ST7701_WriteData(0x06); ST7701_WriteData(0x06); ST7701_WriteData(0x08); ST7701_WriteData(0x08); ST7701_WriteData(0x22); ST7701_WriteData(0x03); ST7701_WriteData(0x51); ST7701_WriteData(0x11); ST7701_WriteData(0x66); ST7701_WriteData(0x2B); ST7701_WriteData(0x0F);
    ST7701_WriteCommand(0xFF); ST7701_WriteData(0x77); ST7701_WriteData(0x01); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x11);
    ST7701_WriteCommand(0xB0); ST7701_WriteData(0x5D);
    ST7701_WriteCommand(0xB1); ST7701_WriteData(0x3E);
    ST7701_WriteCommand(0xB2); ST7701_WriteData(0x81);
    ST7701_WriteCommand(0xB3); ST7701_WriteData(0x80);
    ST7701_WriteCommand(0xB5); ST7701_WriteData(0x4E);
    ST7701_WriteCommand(0xB7); ST7701_WriteData(0x85);
    ST7701_WriteCommand(0xB8); ST7701_WriteData(0x20);
    ST7701_WriteCommand(0xC1); ST7701_WriteData(0x78);
    ST7701_WriteCommand(0xC2); ST7701_WriteData(0x78);
    ST7701_WriteCommand(0xD0); ST7701_WriteData(0x88);
    ST7701_WriteCommand(0xE0); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x02);
    ST7701_WriteCommand(0xE1); ST7701_WriteData(0x06); ST7701_WriteData(0x30); ST7701_WriteData(0x08); ST7701_WriteData(0x30); ST7701_WriteData(0x05); ST7701_WriteData(0x30); ST7701_WriteData(0x07); ST7701_WriteData(0x30); ST7701_WriteData(0x00); ST7701_WriteData(0x33); ST7701_WriteData(0x33);
    ST7701_WriteCommand(0xE2); ST7701_WriteData(0x11); ST7701_WriteData(0x11); ST7701_WriteData(0x33); ST7701_WriteData(0x33); ST7701_WriteData(0xF4); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0xF4); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0xE3); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x11); ST7701_WriteData(0x11);
    ST7701_WriteCommand(0xE4); ST7701_WriteData(0x44); ST7701_WriteData(0x44);
    ST7701_WriteCommand(0xE5); ST7701_WriteData(0x0D); ST7701_WriteData(0xF5); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x0F); ST7701_WriteData(0xF7); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x09); ST7701_WriteData(0xF1); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x0B); ST7701_WriteData(0xF3); ST7701_WriteData(0x30); ST7701_WriteData(0xF0);
    ST7701_WriteCommand(0xE6); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x11); ST7701_WriteData(0x11);
    ST7701_WriteCommand(0xE7); ST7701_WriteData(0x44); ST7701_WriteData(0x44);
    ST7701_WriteCommand(0xE8); ST7701_WriteData(0x0C); ST7701_WriteData(0xF4); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x0E); ST7701_WriteData(0xF6); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x08); ST7701_WriteData(0xF0); ST7701_WriteData(0x30); ST7701_WriteData(0xF0); ST7701_WriteData(0x0A); ST7701_WriteData(0xF2); ST7701_WriteData(0x30); ST7701_WriteData(0xF0);
    ST7701_WriteCommand(0xE9); ST7701_WriteData(0x36); ST7701_WriteData(0x01);
    ST7701_WriteCommand(0xEB); ST7701_WriteData(0x00); ST7701_WriteData(0x01); ST7701_WriteData(0xE4); ST7701_WriteData(0xE4); ST7701_WriteData(0x44); ST7701_WriteData(0x88); ST7701_WriteData(0x40);
    ST7701_WriteCommand(0xED); ST7701_WriteData(0xFF); ST7701_WriteData(0x10); ST7701_WriteData(0xAF); ST7701_WriteData(0x76); ST7701_WriteData(0x54); ST7701_WriteData(0x2B); ST7701_WriteData(0xCF); ST7701_WriteData(0xFF); ST7701_WriteData(0xFF); ST7701_WriteData(0xFC); ST7701_WriteData(0xB2); ST7701_WriteData(0x45); ST7701_WriteData(0x67); ST7701_WriteData(0xFA); ST7701_WriteData(0x01); ST7701_WriteData(0xFF);
    ST7701_WriteCommand(0xEF); ST7701_WriteData(0x08); ST7701_WriteData(0x08); ST7701_WriteData(0x08); ST7701_WriteData(0x45); ST7701_WriteData(0x3F); ST7701_WriteData(0x54);
    ST7701_WriteCommand(0xFF); ST7701_WriteData(0x77); ST7701_WriteData(0x01); ST7701_WriteData(0x00); ST7701_WriteData(0x00); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0x11);
    delay(120);
    ST7701_WriteCommand(0x3A); ST7701_WriteData(0x66);
    ST7701_WriteCommand(0x36); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0x35); ST7701_WriteData(0x00);
    ST7701_WriteCommand(0x29);

    io_expander.digitalWrite(EXIO_PIN3, HIGH); // CS High
    delay(10);
}

Arduino_RGB_Display *create_waveshare_28C_rgb_panel() {
    // 1. Setup I2C & IO Expander
    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(400000);
    io_expander.begin();
    // Set EXIO1(ST7701 RST), EXIO2(Touch RST), EXIO3(ST7701 CS) as outputs, all LOW initially
    io_expander.modeAll(0x00); // 0 = output
    io_expander.writeAll(0x7F); // 0x7F means EXIO_PIN8 (Buzzer) is LOW, everything else is HIGH

    // 2. ST7701 Reset Sequence
    io_expander.digitalWrite(EXIO_PIN1, LOW); // Reset Low
    delay(10);
    io_expander.digitalWrite(EXIO_PIN1, HIGH); // Reset High
    delay(10);

    // 3. GT911 Touch Reset Sequence with address selection (INT LOW during reset = 0x5D)
    pinMode(TOUCH_INT, OUTPUT);
    digitalWrite(TOUCH_INT, LOW);
    io_expander.digitalWrite(EXIO_PIN2, LOW); // Touch Reset Low
    delay(10);
    io_expander.digitalWrite(EXIO_PIN2, HIGH); // Touch Reset High
    delay(200); // Give it time to boot
    pinMode(TOUCH_INT, INPUT); // Re-assign to input for interrupts
    delay(50);

    // 4. Bit-bang SPI Initialization
    ST7701_Init_Sequence();

    // 4. Create RGB Panel with ESP32-S3 specific timings for this 480x480 panel
    Arduino_ESP32RGBPanel *rgbpanel = new Arduino_ESP32RGBPanel(
        LCD_RGB_DE, LCD_RGB_VSYNC, LCD_RGB_HSYNC, LCD_RGB_PCLK,
        LCD_RGB_D11 /*R0*/, LCD_RGB_D12 /*R1*/, LCD_RGB_D13 /*R2*/, LCD_RGB_D14 /*R3*/, LCD_RGB_D15 /*R4*/,
        LCD_RGB_D5  /*G0*/, LCD_RGB_D6  /*G1*/, LCD_RGB_D7  /*G2*/, LCD_RGB_D8  /*G3*/, LCD_RGB_D9  /*G4*/, LCD_RGB_D10 /*G5*/,
        LCD_RGB_D0  /*B0*/, LCD_RGB_D1  /*B1*/, LCD_RGB_D2  /*B2*/, LCD_RGB_D3  /*B3*/, LCD_RGB_D4  /*B4*/,
        1 /* hsync_polarity */, 40 /* hsync_front_porch */, 8 /* hsync_pulse_width */, 40 /* hsync_back_porch */,
        1 /* vsync_polarity */, 10 /* vsync_front_porch */, 2 /* vsync_pulse_width */, 20 /* vsync_back_porch */,
        // PCLK (Pixel Clock) setting:
        // Set to 16MHz (16000000). A clock that is too fast (e.g. 18MHz) can cause the DMA 
        // to struggle with PSRAM bandwidth, leading to shifting or jittery 'cut' lines.
        0 /* pclk_active_neg */, 16000000 /* prefer_speed */, false /* useBigEndian */,
        0 /* de_idle_high */, 0 /* pclk_idle_high */, 4800 /* bounce_buffer_size */
    );

    Arduino_RGB_Display *gfx = new Arduino_RGB_Display(
        480 /* width */, 480 /* height */, rgbpanel, 0 /* rotation */, true /* auto_flush */
    );

    return gfx;
}
