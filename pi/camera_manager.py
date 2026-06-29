import subprocess
import os
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Exclusive lock for camera access (since camera hardware is a shared resource)
camera_lock = threading.Lock()

def capture_image(output_path="/tmp/last_capture.jpg"):
    """
    Captures a frame from the Raspberry Pi camera.
    Returns:
        bool: True if capture was successful, False otherwise.
    """
    import config
    if not getattr(config, 'CAMERA_ENABLED', False):
        logger.warning("Camera is disabled in config.")
        return False
        
    with camera_lock:
        try:
            # Ensure the output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Remove previous capture if it exists
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception as e:
                    logger.warning(f"Could not remove old capture file: {e}")
                
            cmd = getattr(config, 'CAMERA_CAPTURE_CMD', "rpicam-still -t 100 --immediate -n -o " + output_path)
            
            # Rewrite output path in command if necessary to ensure it matches the argument
            if "-o " in cmd:
                base_cmd = cmd.split("-o ")[0].strip()
                run_cmd = f"{base_cmd} -o {output_path}"
            else:
                run_cmd = f"{cmd} -o {output_path}"
                
            logger.info(f"Executing camera capture: {run_cmd}")
            result = subprocess.run(
                run_cmd, 
                shell=True, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                timeout=8.0
            )
            
            if result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"Successfully captured frame to {output_path}")
                return True
            else:
                err_msg = result.stderr.decode('utf-8', errors='ignore')
                logger.error(f"Camera capture failed (code {result.returncode}): {err_msg}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("Camera capture command timed out.")
            return False
        except Exception as e:
            logger.error(f"Error during camera capture: {e}")
            return False

class PersonDetector:
    """
    Background worker that runs local person detection using picamera2 and the IMX500 hardware accelerator.
    """
    def __init__(self, callback=None):
        self.callback = callback
        self.running = False
        self.thread = None
        self.model_path = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"

    def start(self):
        import config
        if not getattr(config, 'CAMERA_ENABLED', False):
            logger.info("Camera disabled in config. Person detection will not start.")
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Background PersonDetector thread started.")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
            logger.info("Background PersonDetector thread stopped.")

    def _run_loop(self):
        try:
            from picamera2 import Picamera2
            from picamera2.devices.imx500 import IMX500
        except ImportError:
            logger.warning("picamera2 library not available. Local person detection is disabled.")
            return
            
        if not os.path.exists(self.model_path):
            logger.warning(f"IMX500 model file not found at {self.model_path}. Local person detection is disabled.")
            return

        logger.info("Initializing Picamera2 and IMX500 for object detection...")
        try:
            # We acquire the camera lock before opening the Picamera2 stream
            # Note: Running continuous detection will lock the camera resource.
            # To allow capture_image() to run for visual Q&A, we must release the camera
            # or capture frames directly from the Picamera2 stream.
            picam2 = Picamera2()
            
            # Configure camera
            config = picam2.create_preview_configuration(main={"size": (640, 480)})
            picam2.configure(config)
            
            # Load IMX500
            imx500 = IMX500(picam2, model=self.model_path)
            
            picam2.start()
            logger.info("IMX500 detection pipeline started successfully.")
            
            last_person_seen = 0
            
            while self.running:
                # Capture metadata and get inference outputs
                metadata = picam2.capture_metadata()
                outputs = imx500.get_outputs(metadata)
                
                # Check detections (MobileNet SSD post-processing structure)
                # Detections usually return class list, scores, boxes
                if outputs and "detections" in outputs:
                    person_detected = False
                    for detection in outputs["detections"]:
                        # Class 1 is usually 'person' in COCO dataset
                        # SSD MobileNet V2 COCO classes: 1 = person
                        if detection.get("category") == 1 and detection.get("score", 0) > 0.5:
                            person_detected = True
                            break
                            
                    if person_detected:
                        now = time.time()
                        if now - last_person_seen > 10.0:  # Throttle callbacks to every 10s
                            logger.info("Person detected in front of the camera!")
                            last_person_seen = now
                            if self.callback:
                                try:
                                    self.callback()
                                except Exception as e:
                                    logger.error(f"Error in PersonDetector callback: {e}")
                                    
                time.sleep(0.5)  # Run check twice a second
                
            picam2.stop()
            picam2.close()
            
        except Exception as e:
            logger.error(f"Error in PersonDetector run loop: {e}")
            # Ensure resources are released
            try:
                picam2.close()
            except Exception:
                pass
