# OmniOrb Interaction & Hardware Model

This document serves as the technical "steering file" for the OmniOrb interaction logic and hardware topology.

## 1. Hardware Architecture
*   **Rotary Encoder**: Physically connected to the **Raspberry Pi** GPIO pins (BCM 17/27/22).
*   **Display/UI**: Managed by the **ESP32-S3**.
*   **Communication**: The Pi acts as the interaction master. Knob turns are detected by the Pi's `assistant_brains.py` and bridged to the ESP32 via UART commands.

## 2. UART Interaction Bridge
To maintain state synchronization, the following command protocol is used:

| Action | UART Command | ESP32 Response |
| :--- | :--- | :--- |
| **Radar Zoom In** | `Z+` | Increments `range_nm`, saves settings, and triggers `full_redraw()` (if in Radar mode). |
| **Radar Zoom Out** | `Z-` | Decrements `range_nm`, saves settings, and triggers `full_redraw()` (if in Radar mode). |
| **Globe Toggle** | `GLOBE:TOGGLE` | Flips the `is_rotating` flag in `GlobeView.cpp`. |
| **Assistant Style** | `STYLE:TOGGLE` | Toggles between Iris and Face visualizations. |

## 3. UI Stability & Gating
To prevent visual flickering and "leaking" artifacts (e.g., radar lines appearing on the globe):

*   **Function-Level Gating**: `full_redraw()` and `update_radar_sweep()` on the ESP32 are **hard-gated**. They immediately return if `current_app != APP_RADAR`.
*   **Timer Suppression**: The timer ring is explicitly suppressed on `APP_RADAR` and `APP_GLOBE` views to preserve SPI bandwidth and visual fidelity.
*   **Smart Redraw**: The timer ring only updates if the percentage change exceeds 0.2%, eliminating high-frequency flickering.

## 4. Configuration Constants
*   **Rotary Resolution**: 2 steps per detent (configured in `pi/rotary_encoder.py`).
*   **Radar Limits**: 5.0nm (Min) to 250.0nm (Max).
*   **Globe Rotation**: ~30 FPS with a `0.012f` rotation step.

---
*Last Updated: 2026-05-03*
