//!PARAM hl_thresh
//!DESC Lumveil: highlight threshold (luma above this starts compressing)
//!TYPE float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.65

//!PARAM hl_strength
//!DESC Lumveil: highlight compression strength (0 = off)
//!TYPE float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!PARAM hl_chroma_boost
//!DESC Lumveil: chroma restoration beyond neutral preservation (1.0 = neutral, like BT.2390 chroma_correction_scaling)
//!TYPE float
//!MINIMUM 1.0
//!MAXIMUM 2.5
1.3

//!HOOK MAIN
//!BIND HOOKED
//!DESC Lumveil highlight rolloff (chroma-preserving soft knee + chroma restoration)
//!WHEN hl_strength 0.001 >

vec4 hook() {
    vec4 c = HOOKED_texOff(0);
    float luma = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
    if (luma > hl_thresh) {
        float excess     = luma - hl_thresh;
        float gain        = hl_strength * 20.0;
        float compressed = excess / (1.0 + excess * gain);
        float new_luma   = hl_thresh + compressed;
        float ratio       = new_luma / max(luma, 0.0001);
        c.rgb *= ratio;

        // BT.2390のchroma_correction_scalingにならい、単なる比率維持(1.0)を
        // 超えて彩度を積極的に持ち上げる。new_luma(グレー軸)を中心に外側へ広げる。
        vec3 gray = vec3(new_luma);
        c.rgb = clamp(gray + (c.rgb - gray) * hl_chroma_boost, 0.0, 1.0);
    }
    return c;
}
