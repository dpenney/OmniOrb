/**
 * @file ClockView.cpp
 * @brief Complex analog clock UI using LVGL drawing primitives.
 *
 * Implements a high-performance, anti-aliased, polygon-based analog clock face.
 * The hands are drawn dynamically using layered polygons to simulate depth,
 * outlines, and counterweights. Synchronizes all drawing components strictly
 * to a snapshot time structure to ensure visual coherency (no tearing or splitting).
 */
#include "ClockView.h"
#include <time.h>
#include <math.h>

static lv_obj_t *clock_screen = nullptr;
static lv_obj_t *meter = nullptr;
static lv_meter_indicator_t *indic_sec_needle;
static lv_obj_t *date_label;
static lv_obj_t *num_labels[12];
static int _timer_ring_pct = -1;  // -1 = hidden, 0-100 = draw arc

// Global time snapshot to ensure all hands and drawing chunks are perfectly synced
static struct timeval global_frame_tv;
static bool time_synced = false;
static float last_h_angle = -1, last_m_angle = -1, last_s_angle = -1;

// ─── Hand outline colour ─────────────────────────────────────────────────────
// Very dark grey — just visible against the matte black background. Used to
// outline the perimeter of every clock hand so that when hands overlap you can
// still see each individual shape.
#define HAND_OUTLINE lv_color_make(28, 28, 28)
#define HAND_OUTLINE_W 2  // line width in pixels
// Absolute pixel distance ahead of pivot where the grey/white boundary falls.
// Same value for BOTH hands so the gap looks visually consistent.
#define GREY_GAP_PX 30

// Draw a closed polygon outline by connecting consecutive vertices with lines.
// Pass the outer boundary vertices in order (CW or CCW — direction doesn't matter).
static void polyOutline(lv_draw_ctx_t *ctx, const lv_point_t *pts, int n) {
    lv_draw_line_dsc_t d;
    lv_draw_line_dsc_init(&d);
    d.color       = HAND_OUTLINE;
    d.width       = HAND_OUTLINE_W;
    d.opa         = LV_OPA_COVER;
    d.round_start = 0;
    d.round_end   = 0;
    for (int i = 0; i < n; i++) {
        lv_point_t a = pts[i];
        lv_point_t b = pts[(i + 1) % n];
        lv_draw_line(ctx, &d, &a, &b);
    }
}

// ─── drawTriangleTip: The "10,000-ft" (hour) pointer ────────────────────────
//
//  Shape overview (tip → pivot → back):
//
//  POINT ◀──── widens ────  PEAK ──── narrows ──── PIVOT ──── flares ──── FLAT END
//  (white tip)              (widest)              (moderate)            (dark grey, flat)
//
//  Cross-section widths along the axis (positive = toward tip, negative = toward back):
//
//   -back_len         0 (pivot)     +peak_dist          +len
//      ↑                  ↑              ↑                ↑
//   flat rear          w_pivot       w_peak (max)      tip point
//   (w_rear, flat)
//
//  The forward section (from gap_len onward) is drawn in WHITE.
//  The rear section (from -back_len to +gap_len) is drawn in DARK GREY.
//
//  Drawing order (painter's algorithm — later layers paint on top):
//   1. Full lance pentagon in white (rear → tip)
//   2. Dark grey trapezoid from -back_len → +gap_len (hides white in counterweight area)
//   3. Dark grey filled rect for the flat rear endcap
//
static void drawTriangleTip(lv_draw_ctx_t *draw_ctx, lv_point_t center,
                             float angle_deg, int len, int width, lv_color_t color)
{
    // ── Trig basis vectors ──────────────────────────────────────────────────
    float angle_rad = (angle_deg - 90.0f) * M_PI / 180.0f;

    // Along-hand unit vector (pivot → tip direction)
    float cosA = cos(angle_rad);
    float sinA = sin(angle_rad);

    // Perpendicular unit vector (gives us the hand width)
    float cosP = cos(angle_rad + M_PI / 2.0f);
    float sinP = sin(angle_rad + M_PI / 2.0f);

    // ── Shape control parameters ────────────────────────────────────────────
    // All distances are fractions of 'len' (positive = toward tip, negative = behind)
    float back_len  = len * 0.40f;  // counterweight extends this far behind pivot
    float gap_len   = GREY_GAP_PX;  // absolute px ahead of pivot — same on all hands
    float peak_dist = len * 0.60f;  // where the hand is at its WIDEST (forward)

    // Half-widths at key stations
    float w_peak  = width * 1.4f;   // widest point (the "belly" of the lance)
    float w_pivot = width * 0.7f;   // width right at the pivot
    float w_rear  = width * 1.1f;   // width at the flat back end (flared counterweight)
    float w_gap   = w_pivot + (w_peak - w_pivot) * (gap_len / peak_dist); // interpolated

    // ── Helper: draw a filled triangle ─────────────────────────────────────
    auto tri = [&](lv_point_t a, lv_point_t b, lv_point_t c, lv_color_t col) {
        lv_draw_rect_dsc_t dsc;
        lv_draw_rect_dsc_init(&dsc);
        dsc.bg_color = col;
        dsc.bg_opa   = LV_OPA_COVER;
        lv_point_t pts[3] = {a, b, c};
        lv_draw_polygon(draw_ctx, &dsc, pts, 3);
    };

    // ── Compute named polygon vertices ──────────────────────────────────────
    // Rear flat-end corners (behind pivot)
    lv_point_t rL = { (lv_coord_t)(center.x - back_len*cosA + w_rear*cosP),
                      (lv_coord_t)(center.y - back_len*sinA + w_rear*sinP) };
    lv_point_t rR = { (lv_coord_t)(center.x - back_len*cosA - w_rear*cosP),
                      (lv_coord_t)(center.y - back_len*sinA - w_rear*sinP) };

    // Pivot-width corners
    lv_point_t pL = { (lv_coord_t)(center.x + w_pivot*cosP),
                      (lv_coord_t)(center.y + w_pivot*sinP) };
    lv_point_t pR = { (lv_coord_t)(center.x - w_pivot*cosP),
                      (lv_coord_t)(center.y - w_pivot*sinP) };

    // Peak (widest) corners
    lv_point_t kL = { (lv_coord_t)(center.x + peak_dist*cosA + w_peak*cosP),
                      (lv_coord_t)(center.y + peak_dist*sinA + w_peak*sinP) };
    lv_point_t kR = { (lv_coord_t)(center.x + peak_dist*cosA - w_peak*cosP),
                      (lv_coord_t)(center.y + peak_dist*sinA - w_peak*sinP) };

    // Tip — single point
    lv_point_t TIP = { (lv_coord_t)(center.x + len*cosA),
                       (lv_coord_t)(center.y + len*sinA) };

    // Grey/white boundary corners (at gap_len, interpolated width)
    lv_point_t gL = { (lv_coord_t)(center.x + gap_len*cosA + w_gap*cosP),
                      (lv_coord_t)(center.y + gap_len*sinA + w_gap*sinP) };
    lv_point_t gR = { (lv_coord_t)(center.x + gap_len*cosA - w_gap*cosP),
                      (lv_coord_t)(center.y + gap_len*sinA - w_gap*sinP) };

    // ── Layer 1: Full white forward lance ────────────────────────────────────
    // Covers from the grey/white boundary (+gap_len) to the tip.
    // Shape: trapezoid (gap_len → peak_dist) + triangle (peak_dist → tip)
    tri(gL, gR, kL, color);   // left half of trapezoid
    tri(gR, kR, kL, color);   // right half of trapezoid
    tri(kL, kR, TIP, color);  // triangle taper to the tip

    // ── Layer 2: Full white sweep from pivot to gap (thin visible stub) ─────
    // Draws the very small white section right at the pivot (from 0 to gap_len).
    // This ensures a crisp white region is visible between grey rear and grey front.
    // (Covered mostly by Layer 3 grey, but needed for exact colour boundary.)
    tri(pL, pR, gL, color);
    tri(pR, gR, gL, color);

    // ── Layer 3: Dark grey counterweight region ──────────────────────────────
    // Covers from the rear end (-back_len) forward to just past the pivot (+gap_len).
    // Uses the same dark grey as the stubby for a consistent instrument family look.
    lv_color_t grey = lv_color_make(60, 60, 60);

    // Rear to pivot section (trapezoid, wider at back, narrower at pivot)
    tri(rL, rR, pL, grey);
    tri(rR, pR, pL, grey);

    // Pivot to gap_len section (trapezoid, covering the small forward grey stub)
    tri(pL, pR, gL, grey);
    tri(pR, gR, gL, grey);

    // ── Layer 4: Flat rear end cap ────────────────────────────────────────────
    // The rear end is flat (unlike the stubby's lollipop).
    // Draw a thin filled rectangle across the back to make it visually clean.
    int cap_thickness = 4; // px thickness of the endcap bar
    lv_point_t cL0 = { (lv_coord_t)(center.x - back_len*cosA + w_rear*cosP),
                       (lv_coord_t)(center.y - back_len*sinA + w_rear*sinP) };
    lv_point_t cR0 = { (lv_coord_t)(center.x - back_len*cosA - w_rear*cosP),
                       (lv_coord_t)(center.y - back_len*sinA - w_rear*sinP) };
    lv_point_t cL1 = { (lv_coord_t)(center.x - (back_len + cap_thickness)*cosA + w_rear*cosP),
                       (lv_coord_t)(center.y - (back_len + cap_thickness)*sinA + w_rear*sinP) };
    lv_point_t cR1 = { (lv_coord_t)(center.x - (back_len + cap_thickness)*cosA - w_rear*cosP),
                       (lv_coord_t)(center.y - (back_len + cap_thickness)*sinA - w_rear*sinP) };
    // Outline each layer so the hand is distinguishable when it overlaps others.
    // Outer boundary traced clockwise: TIP → kR → pR → rR → rL → pL → kL → (TIP)
    lv_point_t boundary[] = { TIP, kR, pR, rR, rL, pL, kL };
    polyOutline(draw_ctx, boundary, 7);
}

// ─── drawStubby: The "1,000-ft" (minute) pointer ─────────────────────────────
//
//  Shape overview (travelling from tip → pivot → back):
//
//  ▶ WHITE tip  ──── [small gap] ──── PIVOT ──── DARK GREY body ──── ● LOLLIPOP
//  (pointy triangle)                           (counterweight)        (rear cap)
//
//  Distances along the hand axis (positive = toward tip, negative = toward back):
//
//   -back_len  ──────────────── 0 (pivot) ──── +gap ──────────────── +len
//      ↑                                        ↑                      ↑
//   lollipop center                         grey ends / white begins   white tip
//
//  Drawing order (painters algorithm — later draws on top):
//   1. Full white pentagon (base at -back_len → tip at +len)
//   2. Dark grey rectangle covering -back_len → +gap_len (hides white counterweight section)
//   3. Lollipop circle centred at -back_len (rear cap, slightly wider than hand)
//
static void drawStubby(lv_draw_ctx_t *draw_ctx, lv_point_t center,
                       float angle_deg, int len, int width, lv_color_t color)
{
    // ── Trig basis vectors ──────────────────────────────────────────────────
    // Convert clock-style degrees (0 = 12 o'clock, CW) to math radians
    float angle_rad = (angle_deg - 90.0f) * M_PI / 180.0f;

    // cosA/sinA: unit vector pointing FROM pivot TOWARD the tip
    float cosA = cos(angle_rad);
    float sinA = sin(angle_rad);

    // cosPerp/sinPerp: unit vector perpendicular to the hand axis (for width)
    float cosPerp = cos(angle_rad + M_PI / 2.0f);
    float sinPerp = sin(angle_rad + M_PI / 2.0f);

    // ── Key distances (all measured from pivot, positive = toward tip) ───────
    float back_len  = len * 0.25f; // counterweight extends this far BEHIND pivot
    float gap_len   = GREY_GAP_PX;  // absolute px ahead of pivot — same on all hands
    float body_end  = len * 0.88f; // where the white rectangle ends & taper begins
    // The white pointy tip runs from gap_len → body_end → len (the sharp tip)

    // ── Shared polygon draw descriptor ──────────────────────────────────────
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_opa = LV_OPA_COVER;

    // ── Layer 1: Full white pentagon ─────────────────────────────────────────
    // Covers the full extent from the rear (-back_len) to the pointed tip (+len).
    // Most of the rear section will be painted over by the dark grey in Layer 2,
    // but we draw the whole thing first so the white taper region is correct.
    {
        dsc.bg_color = color; // caller-supplied white

        // Rear base corners (behind the pivot)
        lv_point_t q0 = { (lv_coord_t)(center.x + back_len * (-cosA) + (width/2)*cosPerp),
                          (lv_coord_t)(center.y + back_len * (-sinA) + (width/2)*sinPerp) };
        lv_point_t q1 = { (lv_coord_t)(center.x + back_len * (-cosA) - (width/2)*cosPerp),
                          (lv_coord_t)(center.y + back_len * (-sinA) - (width/2)*sinPerp) };

        // Shoulder corners: where the rectangle meets the taper (ahead of pivot)
        lv_point_t q4 = { (lv_coord_t)(center.x + body_end*cosA + (width/2)*cosPerp),
                          (lv_coord_t)(center.y + body_end*sinA + (width/2)*sinPerp) };
        lv_point_t q2 = { (lv_coord_t)(center.x + body_end*cosA - (width/2)*cosPerp),
                          (lv_coord_t)(center.y + body_end*sinA - (width/2)*sinPerp) };

        // The single sharp tip point
        lv_point_t q3 = { (lv_coord_t)(center.x + len*cosA),
                          (lv_coord_t)(center.y + len*sinA) };

        // Fan triangulation from rear-left corner: covers full pentagon
        lv_point_t t1[3] = {q0, q1, q2};
        lv_point_t t2[3] = {q0, q2, q3};
        lv_point_t t3[3] = {q0, q3, q4};
        lv_draw_polygon(draw_ctx, &dsc, t1, 3);
        lv_draw_polygon(draw_ctx, &dsc, t2, 3);
        lv_draw_polygon(draw_ctx, &dsc, t3, 3);
    }

    // ── Layer 2: Dark grey counterweight rectangle ────────────────────────────
    // Covers from the rear end (-back_len) FORWARD to just +gap_len ahead of pivot.
    // This hides the white paint in the counterweight section, leaving only the
    // forward white body (from +gap_len to the tip) visible.
    {
        lv_color_t dark_grey = lv_color_make(60, 60, 60);
        dsc.bg_color = dark_grey;

        // Rear corners of the grey block (same as white pentagon rear)
        lv_point_t g0 = { (lv_coord_t)(center.x + back_len*(-cosA) + (width/2)*cosPerp),
                          (lv_coord_t)(center.y + back_len*(-sinA) + (width/2)*sinPerp) };
        lv_point_t g1 = { (lv_coord_t)(center.x + back_len*(-cosA) - (width/2)*cosPerp),
                          (lv_coord_t)(center.y + back_len*(-sinA) - (width/2)*sinPerp) };

        // Front corners of the grey block (stopping just ahead of pivot = +gap_len)
        lv_point_t g2 = { (lv_coord_t)(center.x + gap_len*cosA - (width/2)*cosPerp),
                          (lv_coord_t)(center.y + gap_len*sinA - (width/2)*sinPerp) };
        lv_point_t g3 = { (lv_coord_t)(center.x + gap_len*cosA + (width/2)*cosPerp),
                          (lv_coord_t)(center.y + gap_len*sinA + (width/2)*sinPerp) };

        // Two triangles cover the rectangle
        lv_point_t r1[3] = {g0, g1, g2};
        lv_point_t r2[3] = {g0, g2, g3};
        lv_draw_polygon(draw_ctx, &dsc, r1, 3);
        lv_draw_polygon(draw_ctx, &dsc, r2, 3);
    }

    // ── Layer 3: Lollipop circle — caps the rear (counterweight) end ─────────
    // Centre is at -back_len from the pivot (the very back of the hand).
    // Radius is slightly larger than the hand half-width so it protrudes on
    // both sides, making it visually read as a deliberate design element.
    {
        lv_color_t dark_grey = lv_color_make(60, 60, 60);
        int lollipop_r = (width / 2) + 4; // a bit wider than the hand body

        // Position of the lollipop centre (the end of the counterweight)
        int lx = (int)(center.x - back_len * cosA);
        int ly = (int)(center.y - back_len * sinA);

        lv_draw_rect_dsc_t circle_dsc;
        lv_draw_rect_dsc_init(&circle_dsc);
        circle_dsc.bg_color = dark_grey;
        circle_dsc.radius   = LV_RADIUS_CIRCLE; // renders the bounding rect as a circle
        circle_dsc.bg_opa   = LV_OPA_COVER;

        lv_area_t circle_area = {
            (lv_coord_t)(lx - lollipop_r),
            (lv_coord_t)(ly - lollipop_r),
            (lv_coord_t)(lx + lollipop_r),
            (lv_coord_t)(ly + lollipop_r)
        };
        lv_draw_rect(draw_ctx, &circle_dsc, &circle_area); // draw the lollipop fill
    }

    // ── Outline: perimeter of the full stubby shape ───────────────────────────
    // Traced CW: rear-left → rear-right → shoulder-right → tip → shoulder-left
    // This gives a clean border around the entire hand for overlap differentiation.
    lv_point_t rL2 = { (lv_coord_t)(center.x - back_len*cosA + (width/2)*cosPerp),
                       (lv_coord_t)(center.y - back_len*sinA + (width/2)*sinPerp) };
    lv_point_t rR2 = { (lv_coord_t)(center.x - back_len*cosA - (width/2)*cosPerp),
                       (lv_coord_t)(center.y - back_len*sinA - (width/2)*sinPerp) };
    lv_point_t shL = { (lv_coord_t)(center.x + body_end*cosA + (width/2)*cosPerp),
                       (lv_coord_t)(center.y + body_end*sinA + (width/2)*sinPerp) };
    lv_point_t shR = { (lv_coord_t)(center.x + body_end*cosA - (width/2)*cosPerp),
                       (lv_coord_t)(center.y + body_end*sinA - (width/2)*sinPerp) };
    lv_point_t tipS = { (lv_coord_t)(center.x + len*cosA),
                        (lv_coord_t)(center.y + len*sinA) };
    lv_point_t bnd[] = { rL2, rR2, shR, tipS, shL };
    polyOutline(draw_ctx, bnd, 5);

    // Outline the lollipop circle using LVGL's rect border
    {
        lv_color_t dark_grey3 = lv_color_make(60, 60, 60);
        int lollipop_r2 = (width / 2) + 4;
        int lx2 = (int)(center.x - back_len * cosA);
        int ly2 = (int)(center.y - back_len * sinA);
        lv_draw_rect_dsc_t lolOutDsc;
        lv_draw_rect_dsc_init(&lolOutDsc);
        lolOutDsc.bg_color     = dark_grey3;
        lolOutDsc.radius       = LV_RADIUS_CIRCLE;
        lolOutDsc.bg_opa       = LV_OPA_COVER;
        lolOutDsc.border_color = HAND_OUTLINE;
        lolOutDsc.border_width = HAND_OUTLINE_W;
        lolOutDsc.border_opa   = LV_OPA_COVER;
        lv_area_t lolArea2 = {
            (lv_coord_t)(lx2 - lollipop_r2), (lv_coord_t)(ly2 - lollipop_r2),
            (lv_coord_t)(lx2 + lollipop_r2), (lv_coord_t)(ly2 + lollipop_r2)
        };
        lv_draw_rect(draw_ctx, &lolOutDsc, &lolArea2);
    }
}

// Helper for needle with counterweight (Uses polygons for sub-pixel anti-aliasing)
static void drawNeedleWithWeight(lv_draw_ctx_t *draw_ctx, lv_point_t center, float angle_deg, int len, lv_color_t color) {
    float angle_rad = (angle_deg - 90.0f) * M_PI / 180.0f;
    float cosA = cos(angle_rad);
    float sinA = sin(angle_rad);

    // Perpendicular vectors for width
    float cosP = cos(angle_rad + M_PI / 2.0f);
    float sinP = sin(angle_rad + M_PI / 2.0f);

    lv_color_t dark_grey = lv_color_make(60, 60, 60);
    float gap_len = GREY_GAP_PX; // Match the 30px gap on other hands
    float back_limit = len * 0.25f;

    // Widths
    float w_pivot = 2.5f; // 5px total width at base
    float w_tip   = 1.0f; // 2px total width at tip

    lv_draw_rect_dsc_t poly_dsc;
    lv_draw_rect_dsc_init(&poly_dsc);
    poly_dsc.bg_opa = LV_OPA_COVER;

    // ── Points along center axis ──────────────────────────────────────────
    lv_point_t cBack = {(lv_coord_t)(center.x - back_limit * cosA), (lv_coord_t)(center.y - back_limit * sinA)};
    lv_point_t cPivot= center;
    lv_point_t cGap  = {(lv_coord_t)(center.x + gap_len * cosA),    (lv_coord_t)(center.y + gap_len * sinA)};
    lv_point_t cTip  = {(lv_coord_t)(center.x + len * cosA),        (lv_coord_t)(center.y + len * sinA)};

    // Width vectors for left/right points
    lv_point_t pB_L = {(lv_coord_t)(cBack.x + w_pivot*cosP), (lv_coord_t)(cBack.y + w_pivot*sinP)};
    lv_point_t pB_R = {(lv_coord_t)(cBack.x - w_pivot*cosP), (lv_coord_t)(cBack.y - w_pivot*sinP)};

    lv_point_t pP_L = {(lv_coord_t)(cPivot.x + w_pivot*cosP), (lv_coord_t)(cPivot.y + w_pivot*sinP)};
    lv_point_t pP_R = {(lv_coord_t)(cPivot.x - w_pivot*cosP), (lv_coord_t)(cPivot.y - w_pivot*sinP)};

    // Width at the gap (interpolated between pivot and tip)
    float w_gap = w_pivot - ((w_pivot - w_tip) * (gap_len / len));
    lv_point_t pG_L = {(lv_coord_t)(cGap.x + w_gap*cosP), (lv_coord_t)(cGap.y + w_gap*sinP)};
    lv_point_t pG_R = {(lv_coord_t)(cGap.x - w_gap*cosP), (lv_coord_t)(cGap.y - w_gap*sinP)};

    lv_point_t pT_L = {(lv_coord_t)(cTip.x + w_tip*cosP), (lv_coord_t)(cTip.y + w_tip*sinP)};
    lv_point_t pT_R = {(lv_coord_t)(cTip.x - w_tip*cosP), (lv_coord_t)(cTip.y - w_tip*sinP)};

    // ── Outline covering BOTH segments ───────────────────────────────────────
    // We outline the total bounding shape BEFORE drawing the fill so the bright colors sit on top
    lv_point_t full_outline[6] = {pB_L, pB_R, pT_R, pT_L};
    polyOutline(draw_ctx, full_outline, 4);

    // ── Segment 1: The rear/pivot area (Dark Grey) ───────────────────────────
    poly_dsc.bg_color = dark_grey;
    lv_point_t pts_rear[4] = {pB_L, pB_R, pP_R, pP_L};
    lv_point_t pts_mid[4]  = {pP_L, pP_R, pG_R, pG_L};
    // Draw via two triangles per trapezoid to ensure LVGL renders it solidly
    lv_point_t tR1[3] = {pB_L, pB_R, pP_R}; lv_point_t tR2[3] = {pB_L, pP_R, pP_L};
    lv_point_t tM1[3] = {pP_L, pP_R, pG_R}; lv_point_t tM2[3] = {pP_L, pG_R, pG_L};
    lv_draw_polygon(draw_ctx, &poly_dsc, tR1, 3); lv_draw_polygon(draw_ctx, &poly_dsc, tR2, 3);
    lv_draw_polygon(draw_ctx, &poly_dsc, tM1, 3); lv_draw_polygon(draw_ctx, &poly_dsc, tM2, 3);

    // ── Segment 2: The tip area (Orange) ─────────────────────────────────────
    poly_dsc.bg_color = color;
    lv_point_t tF1[3] = {pG_L, pG_R, pT_R}; lv_point_t tF2[3] = {pG_L, pT_R, pT_L};
    lv_draw_polygon(draw_ctx, &poly_dsc, tF1, 3); lv_draw_polygon(draw_ctx, &poly_dsc, tF2, 3);


    // ── Counterweight Circle (Dark Grey) ─────────────────────────────────────
    lv_draw_rect_dsc_t circle_dsc;
    lv_draw_rect_dsc_init(&circle_dsc);
    circle_dsc.bg_color = dark_grey;
    circle_dsc.radius = LV_RADIUS_CIRCLE;
    int r = 10;
    lv_area_t area = {(lv_coord_t)(cBack.x-r), (lv_coord_t)(cBack.y-r), (lv_coord_t)(cBack.x+r), (lv_coord_t)(cBack.y+r)};
    
    // Outline for the counterweight circle
    lv_draw_rect_dsc_t circle_out_dsc = circle_dsc;
    circle_out_dsc.bg_opa = LV_OPA_TRANSP;
    circle_out_dsc.border_color = HAND_OUTLINE;
    circle_out_dsc.border_width = HAND_OUTLINE_W;
    circle_out_dsc.border_opa = LV_OPA_COVER;
    
    lv_draw_rect(draw_ctx, &circle_dsc, &area);
    lv_draw_rect(draw_ctx, &circle_out_dsc, &area);
}

static void meter_draw_event_cb(lv_event_t * e) {
    lv_event_code_t code = lv_event_get_code(e);
    if(code == LV_EVENT_DRAW_POST) {
        lv_draw_ctx_t * draw_ctx = lv_event_get_draw_ctx(e);
        lv_point_t center = {240, 240};
        
        // ── Drawing Sync ─────────────────────────────────────────────────────
        // We use the global_frame_tv snapshot locked in ClockView::update_time()
        // to ensure that all chunks of the same frame use identical angles.
        // NO sampling happens here to prevent "hand splitting" artifacts.

        struct tm *tm_info = localtime(&global_frame_tv.tv_sec);
        if (!tm_info) return;

        float h_angle = (tm_info->tm_hour % 12 * 30.0f + tm_info->tm_min * 0.5f);
        float m_angle = (tm_info->tm_min * 6.0f + tm_info->tm_sec * 0.1f);
        
        // ── Second Hand: Continuous Smooth Sweep ─────────
        // Use the raw microsecond fraction for a completely continuous sweep at 60 FPS
        float fraction = global_frame_tv.tv_usec / 1000000.0f;
        float s_angle = (tm_info->tm_sec + fraction) * 6.0f;


        drawTriangleTip(draw_ctx, center, h_angle, 120, 10, lv_color_white()); // 10k ft
        drawStubby(draw_ctx, center, m_angle, 205, 22, lv_color_white());     // 1k ft (reach ticks)
        drawNeedleWithWeight(draw_ctx, center, s_angle, 230, lv_palette_main(LV_PALETTE_ORANGE)); // 100 ft

        lv_draw_rect_dsc_t hub_dsc;
        lv_draw_rect_dsc_init(&hub_dsc);
        hub_dsc.bg_color = lv_color_make(60, 60, 60); // Dark grey pivot dot
        hub_dsc.radius = LV_RADIUS_CIRCLE;
        lv_area_t hub_area = {240-10, 240-10, 240+10, 240+10};
        lv_draw_rect(draw_ctx, &hub_dsc, &hub_area);
        hub_dsc.bg_color = lv_color_black();
        lv_area_t inner_hub = {240-4, 240-4, 240+4, 240+4};
        lv_draw_rect(draw_ctx, &hub_dsc, &inner_hub);

        // ── Timer ring — outer edge of display ──────────────────────────────
        if (_timer_ring_pct >= 0) {
            lv_color_t ring_col;
            if      (_timer_ring_pct >= 50) ring_col = lv_palette_main(LV_PALETTE_GREEN);
            else if (_timer_ring_pct >= 20) ring_col = lv_palette_main(LV_PALETTE_YELLOW);
            else                            ring_col = lv_palette_main(LV_PALETTE_RED);

            lv_draw_arc_dsc_t arc_dsc;
            lv_draw_arc_dsc_init(&arc_dsc);
            arc_dsc.color = ring_col;
            arc_dsc.width = 10;
            arc_dsc.opa   = LV_OPA_COVER;

            // LVGL arc angles: 0=right(3 o'clock), clockwise.
            // 270 = 12 o'clock. Span = pct/100 * 360.
            uint16_t end_angle = (uint16_t)(270 + (_timer_ring_pct * 360 / 100)) % 360;
            lv_draw_arc(draw_ctx, &arc_dsc, &center, 233, 270, end_angle);
        }
    }
}

void ClockView::init() {
    if (clock_screen) return;

    clock_screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(clock_screen, lv_color_black(), 0);

    meter = lv_meter_create(clock_screen);
    lv_obj_set_size(meter, 480, 480);
    lv_obj_center(meter);
    lv_obj_set_style_bg_opa(meter, LV_OPA_TRANSP, 0); // Transparent so labels behind are visible
    lv_obj_set_style_border_width(meter, 0, 0);

    lv_meter_scale_t *scale = lv_meter_add_scale(meter);
    lv_meter_set_scale_ticks(meter, scale, 61, 2, 20, lv_color_make(100, 100, 100)); 
    lv_meter_set_scale_major_ticks(meter, scale, 5, 5, 30, lv_color_white(), 0); 
    lv_meter_set_scale_range(meter, scale, 0, 60, 360, 270);

    for(int i = 0; i < 12; i++) {
        float angle = (i * 30.0f - 90.0f) * M_PI / 180.0f;
        int r = 160;
        int lx = 240 + r * cos(angle);
        int ly = 240 + r * sin(angle);

        num_labels[i] = lv_label_create(clock_screen);
        char buf[4];
        snprintf(buf, sizeof(buf), "%d", i);
        lv_label_set_text(num_labels[i], buf);
        lv_obj_set_style_text_font(num_labels[i], &lv_font_montserrat_40, 0);
        lv_obj_set_style_text_color(num_labels[i], lv_color_white(), 0);
        lv_obj_align(num_labels[i], LV_ALIGN_CENTER, lx - 240, ly - 240);
    }

    lv_obj_add_event_cb(meter, meter_draw_event_cb, LV_EVENT_DRAW_POST, NULL);

    date_label = lv_label_create(clock_screen);
    lv_obj_set_style_text_font(date_label, &lv_font_montserrat_32, 0);
    lv_obj_set_style_text_color(date_label, lv_color_white(), 0);
    lv_obj_set_style_bg_color(date_label, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(date_label, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(date_label, lv_color_white(), 0);
    lv_obj_set_style_border_width(date_label, 2, 0);
    lv_obj_set_style_pad_all(date_label, 4, 0);
    lv_obj_align(date_label, LV_ALIGN_CENTER, 85, 0); 
    
    lv_obj_t *brand = lv_label_create(clock_screen);
    lv_label_set_text(brand, "ALT");
    lv_obj_set_style_text_font(brand, &lv_font_montserrat_32, 0);
    lv_obj_set_style_text_color(brand, lv_color_white(), 0);
    lv_obj_align(brand, LV_ALIGN_CENTER, 0, -60);

    // Layering: Move the meter (which draws the hands) to the foreground so hands
    // are on top of all labels. Background opa set to 0 and transparent.
    lv_obj_set_style_bg_opa(meter, LV_OPA_TRANSP, 0);
    lv_obj_move_foreground(meter);
    // Timer ring is drawn inside meter_draw_event_cb — no extra widget needed.
}

// Helper to invalidate ONLY the area where a hand is (old or new position)
static void invalidate_hand_area(lv_obj_t *obj, float angle, int len, int width) {
    if (angle < 0) return;
    float rad = (angle - 90.0f) * M_PI / 180.0f;
    float cosA = cos(rad);
    float sinA = sin(rad);
    
    // Calculate points for tip and counterweight tail
    int x1 = 240 + (int)((len + 5) * cosA);
    int y1 = 240 + (int)((len + 5) * sinA);
    int x2 = 240 - (int)((len * 0.45f) * cosA); 
    int y2 = 240 - (int)((len * 0.45f) * sinA);
    
    lv_area_t area;
    area.x1 = (lv_coord_t)(min(x1, x2) - 30); // 30px padding for safety
    area.y1 = (lv_coord_t)(min(y1, y2) - 30);
    area.x2 = (lv_coord_t)(max(x1, x2) + 30);
    area.y2 = (lv_coord_t)(max(y1, y2) + 30);
    lv_obj_invalidate_area(obj, &area);
}

void ClockView::show() {
    init();
    lv_scr_load(clock_screen);
    update_time();
}

void ClockView::hide() {}

void ClockView::set_timer_pct(int pct) {
    if (pct == _timer_ring_pct) return;   // skip if unchanged
    _timer_ring_pct = pct;
    if (meter) lv_obj_invalidate(meter);  // ask LVGL to redraw the meter area
}

void ClockView::update_time() {
    if (!clock_screen) return;

    // Reset the sync flag so the next draw round takes a fresh time snapshot.
    time_synced = false;

    // Update the date window only once per second for efficiency.
    static int last_sec = -1;
    struct timeval tv;
    gettimeofday(&tv, NULL);
    struct tm* tm_ptr = localtime(&tv.tv_sec);
    if (tm_ptr && tm_ptr->tm_sec != last_sec) {
        char buf[8];
        snprintf(buf, sizeof(buf), "%02d.%02d", tm_ptr->tm_mon + 1, tm_ptr->tm_mday);
        lv_label_set_text(date_label, buf);
        last_sec = tm_ptr->tm_sec;
    }

    if (meter) {
        struct timeval now_tv;
        gettimeofday(&now_tv, NULL);
        struct tm* tm_ptr = localtime(&now_tv.tv_sec);
        if (!tm_ptr) return;

        float fraction = now_tv.tv_usec / 1000000.0f;

        float h_angle = (tm_ptr->tm_hour % 12 * 30.0f + tm_ptr->tm_min * 0.5f);
        float m_angle = (tm_ptr->tm_min * 6.0f + tm_ptr->tm_sec * 0.1f);
        float s_angle = (tm_ptr->tm_sec + fraction) * 6.0f;

        // If this is the very first update, initialize the global snapshot
        if (last_s_angle < 0) {
            global_frame_tv = now_tv;
        }

        // Only invalidate if angles actually changed
        if (h_angle != last_h_angle || m_angle != last_m_angle || s_angle != last_s_angle) {
            // Lock the time snapshot for the upcoming draw callback
            global_frame_tv = now_tv;

            // Invalidate OLD positions
            invalidate_hand_area(meter, last_h_angle, 120, 15);
            invalidate_hand_area(meter, last_m_angle, 205, 25);
            invalidate_hand_area(meter, last_s_angle, 230, 20);

            // Invalidate NEW positions
            invalidate_hand_area(meter, h_angle, 120, 15);
            invalidate_hand_area(meter, m_angle, 205, 25);
            invalidate_hand_area(meter, s_angle, 230, 20);

            last_h_angle = h_angle;
            last_m_angle = m_angle;
            last_s_angle = s_angle;

            // Occasional date update
            static int last_day = -1;
            if (tm_ptr->tm_mday != last_day) {
                char buf[8];
                snprintf(buf, sizeof(buf), "%02d.%02d", tm_ptr->tm_mon + 1, tm_ptr->tm_mday);
                lv_label_set_text(date_label, buf);
                last_day = tm_ptr->tm_mday;
            }
        }
    }
}
