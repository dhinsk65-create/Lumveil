//!PARAM shadow_lift
//!DESC Lumveil: shadow-only luminance lift (0 = off, 1 = max lift)
//!TYPE float
//!MINIMUM 0.0
//!MAXIMUM 1.0
0.0

//!HOOK MAIN
//!BIND HOOKED
//!DESC Lumveil shadow lift that brightens only dark pixels, protecting mid/highlights
//!WHEN shadow_lift 0.001 >

vec4 hook() {
    vec4 c = HOOKED_texOff(0);
    float luma = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
    // 有理関数トーンカーブ f(x) = (1+k)x / (1+kx) は f(0)=0 で単調増加。
    // 黒(レターボックス・ノイズフロア)は持ち上げず、暗部だけを増幅する。
    float k = 3.0 * shadow_lift;
    // 輝度0.5以上は重み0(中間調・ハイライト保護)。持ち上げ量は常に正なので
    // 単調な2曲線の正係数ブレンドとなり、階調の逆転が起きない。
    float w = clamp(1.0 - luma / 0.5, 0.0, 1.0);
    w = w * w;
    // カーブは輝度ゲイン乗算ではなくチャンネル別に適用する。輝度はBの寄与が
    // 7%しかないため、輝度ベースの一律ゲインだと「輝度は極小だがBだけ大きい」
    // 暗部の青みが最大4倍近く増幅されて青白く飛ぶ(実測・数値シミュレーションで
    // 確認済み)。チャンネル別なら各成分が自身のカーブ(上限1未満)で頭打ちになる。
    vec3 lifted = (1.0 + k) * c.rgb / (1.0 + k * c.rgb);
    c.rgb = clamp(mix(c.rgb, lifted, w), 0.0, 1.0);
    return c;
}
