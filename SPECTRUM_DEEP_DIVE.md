# Deep Dive: OmniOrb Spectrum Analyzer Logic

This document analyzes the current implementation of the Iris Mode Spectrum Analyzer and provides a roadmap for achieving artifact-free, high-performance rendering.

## 1. Current Logic Overview
The spectrum analyzer visualizes 16 frequency bins (`freq_bins`) as 32 radial bars (mirrored 180 degrees) around the perimeter of the 480x480 circular display.

### Rendering Workflow (Per Frame):
1.  **Data Processing**: Each bin is scaled by `max_len (90px)` and smoothed using an `AUDIO_DECAY` of 0.88.
2.  **Coordinate Calculation**: For each bin, the code calculates the start `(x0, y0)` and end `(x1, y1)` points using:
    *   `x = CX + cos(rad) * radius`
    *   `y = CY + sin(rad) * radius`
3.  **Drawing**: Each "bar" is actually a sweep of ~9 individual `drawLine` calls spaced 0.5 degrees apart to create a wedge shape.

## 2. Why it is "Sluggish"
The sluggishness is not a lack of CPU power, but **SPI Bus Congestion**.

*   **Data Volume**: A 480x480 16-bit canvas is **460,800 bytes**. 
*   **SPI bottleneck**: At 80MHz, the theoretical max is ~22 FPS. However, when we force a "Full Flush" (current Iris behavior), we saturate the bus.
*   **Trig Overhead**: We are performing 288 `sinf()` and 288 `cosf()` calls every frame. While the ESP32-S3 is fast, doing this in the main UI thread adds micro-jitter.

## 3. Why Erasing is Failing
The "Annulus Wipe" using `drawCircle` failed because of **Rasterization Discrepancy**.
*   **Line vs Circle**: The Bresenham algorithm for lines and the Midpoint algorithm for circles calculate diagonal pixels differently. A circle of radius 200 will not perfectly cover a line ending at distance 200.
*   **Accumulation**: Because the erase missed ~5% of the pixels, they accumulated over time into "ghost bars."

## 4. Suggested Improvements

### A. The "Mirror-Map" LUT (Performance)
Pre-calculate a Lookup Table (LUT) of `float cos_vals[288]` and `float sin_vals[288]`.
*   **Benefit**: Reduces 576 trig calls to 576 simple array lookups.
*   **Result**: 10-15% reduction in frame compute time.

### B. Segmented Flushing (Bandwidth)
The Iris view should adopt the "Face" view's optimization:
*   Only flush `y=40` to `y=440`.
*   By skipping the top and bottom 40 blank pixels, we save 38,400 pixels of transfer per frame, potentially gaining 3-5 FPS.

### C. Differential Line Erasure (Correctness)
Instead of circles, we must use the **exact same loop** for erasing and drawing.
```cpp
// Erase Loop
drawLine(x0, y0, x1_old, y1_old, C_BG);
// Update Magnitude
mag = new_mag;
// Draw Loop
drawLine(x0, y0, x1_new, y1_new, C_ACCENT);
```
By using the same `rad` and `start_r`, we guarantee the rasterizer hits the same pixels.

### D. Magnitude Gating
Only redraw a bin if its magnitude has changed by more than 1 pixel. This prevents redundant SPI writes for static or near-silent audio.
