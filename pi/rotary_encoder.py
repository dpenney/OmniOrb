import threading
import time
import config

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    GPIO = None

class RotaryEncoder:
    def __init__(self, clk_pin, dt_pin, sw_pin=None, callback=None):
        self.clk_pin = clk_pin
        self.dt_pin = dt_pin
        self.sw_pin = sw_pin
        self.callback = callback
        self.value = 0
        self.running = False
        
        # Long press and debouncing settings
        self.long_press_ms = 2000
        self.debounce_ms = 50
        self.sw_pressed_at = 0
        self.sw_last_state = 1 # Default to Pull-up active (Idle)
        self.long_press_fired = False

        if GPIO:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.clk_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                GPIO.setup(self.dt_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                if self.sw_pin:
                    GPIO.setup(self.sw_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                    # Use current state as baseline
                    self.sw_last_state = GPIO.input(self.sw_pin)

                self.last_clk_state = GPIO.input(self.clk_pin)
                self.running = True
                
                # Start polling thread
                self.thread = threading.Thread(target=self._poll, daemon=True)
                self.thread.start()
            except Exception as e:
                print(f"RotaryEncoder hardware init failed: {e}")
                self.running = False

    def _poll(self):
        while self.running:
            # 1. Rotation Check
            clk_state = GPIO.input(self.clk_pin)
            dt_state = GPIO.input(self.dt_pin)
            
            if clk_state != self.last_clk_state:
                if dt_state != clk_state:
                    self.value += 1
                    direction = "CW"
                else:
                    self.value -= 1
                    direction = "CCW"
                
                if self.callback:
                    self.callback("rotate", direction, self.value)
                
            self.last_clk_state = clk_state
            
            # 2. Switch Check (Polling with Debounce and Long-Press)
            if self.sw_pin:
                sw_state = GPIO.input(self.sw_pin)
                now_ms = time.time() * 1000
                
                if sw_state == 0 and self.sw_last_state == 1:
                    # Press down (Transition 1 -> 0)
                    self.sw_pressed_at = now_ms
                    self.long_press_fired = False
                elif sw_state == 1 and self.sw_last_state == 0:
                    # Release (Transition 0 -> 1)
                    if self.sw_pressed_at > 0:
                        duration = now_ms - self.sw_pressed_at
                        # Only fire press if it wasn't a long press and exceeds debounce threshold
                        if not self.long_press_fired and duration >= self.debounce_ms:
                            if self.callback:
                                self.callback("press", None, None)
                    self.sw_pressed_at = 0
                elif sw_state == 0 and self.sw_last_state == 0:
                    # Still holding (State 0)
                    if not self.long_press_fired and self.sw_pressed_at > 0:
                        if (now_ms - self.sw_pressed_at) >= self.long_press_ms:
                            self.long_press_fired = True
                            if self.callback:
                                self.callback("long_press", None, None)
                
                self.sw_last_state = sw_state
                
            time.sleep(config.ENCODER_POLL_SLEEP)

    def stop(self):
        self.running = False
        self.thread.join()

if __name__ == "__main__":
    def test_cb(event, direction, value):
        print(f"Event: {event}, Dir: {direction}, Value: {value}")
        
    try:
        encoder = RotaryEncoder(
            clk_pin=config.PIN_ROTARY_CLK, 
            dt_pin=config.PIN_ROTARY_DT, 
            sw_pin=config.PIN_ROTARY_SW,
            callback=test_cb
        )
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        GPIO.cleanup()
