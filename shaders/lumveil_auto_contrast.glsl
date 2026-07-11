//!PARAM auto_contrast
//!DESC Lumveil: contrast extension beyond MPV +100 (0 = off, 1 = +200, 2 = +300 equivalent)
//!TYPE float
//!MINIMUM 0.0
//!MAXIMUM 2.0
0.0

//!HOOK MAIN
//!BIND HOOKED
//!DESC Lumveil contrast extension that follows MPV's brighter contrast direction
//!WHEN auto_contrast 0.001 >

vec4 hook() {
    vec4 c = HOOKED_texOff(0);
    // MPVのコントラストを+100まで適用した出力を入力として、その上がり方を
    // 延長する。ゲインと正のオフセットを併用し、追加分で画面全体が暗くなる
    // 以前の固定S字カーブを避ける。
    float gain = 1.0 + 0.35 * auto_contrast;
    float lift = 0.08 * auto_contrast;
    c.rgb = clamp(c.rgb * gain + lift, 0.0, 1.0);
    return c;
}
