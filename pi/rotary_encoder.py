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
        
        if GPIO:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.clk_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                GPIO.setup(self.dt_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                if self.sw_pin:
                    GPIO.setup(self.sw_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

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
            
            # Switch check
            if self.sw_pin:
                # Add simple switch polling or keep interrupt for switch
                pass
                
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
