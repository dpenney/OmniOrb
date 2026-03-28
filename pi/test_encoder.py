import RPi.GPIO as GPIO
import time
import config

# Pin Definitions from config
CLK_PIN = config.PIN_ROTARY_CLK
DT_PIN  = config.PIN_ROTARY_DT
GND_PIN = config.PIN_SFT_GND

GPIO.setmode(GPIO.BCM)

# Set up GND_PIN as a software ground
GPIO.setup(GND_PIN, GPIO.OUT)
GPIO.output(GND_PIN, GPIO.LOW)

# Set up CLK and DT as inputs with pull-up resistors
GPIO.setup(CLK_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(DT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

counter = 0
clk_last_state = GPIO.input(CLK_PIN)

print(f"Rotary Encoder Test Started (CLK={CLK_PIN}, DT={DT_PIN}, GND={GND_PIN})")
print("Turn the encoder to see the value change. Press Ctrl+C to exit.")

try:
    while True:
        clk_state = GPIO.input(CLK_PIN)
        dt_state = GPIO.input(DT_PIN)
        
        if clk_state != clk_last_state:
            if dt_state != clk_state:
                counter += 1
                direction = "Clockwise"
            else:
                counter -= 1
                direction = "Counter-Clockwise"
            
            print(f"Direction: {direction} | Counter: {counter}")
            
        clk_last_state = clk_state
        time.sleep(0.001)  # Small delay for debouncing

except KeyboardInterrupt:
    print("\nTest stopped by user.")
finally:
    GPIO.cleanup()
