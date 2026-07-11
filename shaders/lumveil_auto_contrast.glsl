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
    // 中間点(pivot)を軸に明暗差を広げる、本来のコントラスト拡張。
    // AUTOは暗いシーンでのみこのシェーダーを起動するため、pivotは暗部寄りの
    // 低い値に固定する。0.5等の中間グレーを軸にすると、まだ暗いシーンの
    // 画素の大半がpivotより下になり、コントラストを上げるほど画面全体が
    // 黒つぶれする（旧S字カーブと同じ問題）。0.15付近を軸にすることで、
    // 暗部を潰さずにハイライト側との差を実際に広げられる（実測確認済み）。
    float factor = 1.0 + 0.5 * auto_contrast;
    float pivot = 0.15;
    c.rgb = clamp((c.rgb - pivot) * factor + pivot, 0.0, 1.0);
    return c;
}
