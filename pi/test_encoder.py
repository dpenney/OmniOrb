import RPi.GPIO as GPIO
import time
import config

# Pin Definitions from config
CLK_PIN = config.PIN_ROTARY_CLK
DT_PIN  = config.PIN_ROTARY_DT
SW_PIN  = config.PIN_ROTARY_SW
GND_PIN = config.PIN_SFT_GND

GPIO.setmode(GPIO.BCM)

# Set up GND_PIN as a software ground if needed
if GND_PIN:
    GPIO.setup(GND_PIN, GPIO.OUT)
    GPIO.output(GND_PIN, GPIO.LOW)

# Set up CLK, DT, and SW as inputs with pull-up resistors
GPIO.setup(CLK_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(DT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(SW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

counter = 0
clk_last_state = GPIO.input(CLK_PIN)
sw_last_state = GPIO.input(SW_PIN)

print(f"Rotary Encoder Debug Started")
print(f"Pins: CLK={CLK_PIN}, DT={DT_PIN}, SW={SW_PIN}, GND={GND_PIN}")
print(f"Initial SW State: {sw_last_state}")
print("Press Ctrl+C to exit.")

try:
    while True:
        clk_state = GPIO.input(CLK_PIN)
        dt_state = GPIO.input(DT_PIN)
        sw_state = GPIO.input(SW_PIN)
        
        # Rotation
        if clk_state != clk_last_state:
            if dt_state != clk_state:
                counter += 1
                direction = "Clockwise"
            else:
                counter -= 1
                direction = "Counter-Clockwise"
            print(f"Rotate: {direction} | Counter: {counter}")
            
        # Switch
        if sw_state != sw_last_state:
            print(f"Switch State Changed: {sw_last_state} -> {sw_state} ({'RELEASED' if sw_state else 'PRESSED'})")
            sw_last_state = sw_state
            
        clk_last_state = clk_state
        time.sleep(0.001)

except KeyboardInterrupt:
    print("\nTest stopped by user.")
finally:
    GPIO.cleanup()
