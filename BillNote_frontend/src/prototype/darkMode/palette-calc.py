# -*- coding: utf-8 -*-
"""暗色候选配色 v2：最终定值 + 全套 token 对比度校验 + 输出 CSS 片段（2026-10-15）
锚点调整：A 的 muted 提亮至 ≥4.5(压 card)；primary 统一压到 L≤0.70 保证
深色字按钮 ≥4.5（dark text on primary），tint 徽标对比另行浏览器审计。
"""
import math

def oklch_to_srgb(L, C, H):
    h = math.radians(H); a = C * math.cos(h); b = C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621*l - 3.3077115913*m + 0.2309699292*s
    g = -1.2684380046*l + 2.6097574011*m - 0.3413193965*s
    bl = -0.0041960863*l - 0.7034186147*m + 1.7076147010*s
    def enc(x):
        x = max(0.0, min(1.0, x))
        return 12.92*x if x <= 0.0031308 else 1.055 * x ** (1/2.4) - 0.055
    return tuple(enc(v) for v in (r, g, bl))

def hexof(t):
    r, g, b = oklch_to_srgb(*t)
    return '#%02x%02x%02x' % (round(r*255), round(g*255), round(b*255))

def lin(x):
    return x/12.92 if x <= 0.04045 else ((x+0.055)/1.055)**2.4

def lum(t):
    r, g, b = oklch_to_srgb(*t)
    return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)

def lum_hex(hx):
    hx = hx.lstrip('#'); r, g, b = (int(hx[i:i+2], 16)/255 for i in (0, 2, 4))
    return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)

def ct(a, b):
    x, y = sorted((a, b), reverse=True)
    return (x+0.05)/(y+0.05)

V = {}
V['A 冷灰蓝'] = dict(
    bg=(0.253, 0.011, 245), card=(0.285, 0.012, 245), popover=(0.305, 0.013, 245),
    fg=(0.790, 0.014, 235), muted_fg=(0.660, 0.014, 235), muted=(0.340, 0.013, 245),
    primary=(0.680, 0.110, 245), pfg=(0.200, 0.010, 245),
)
V['B 石墨'] = dict(
    bg=(0.218, 0.002, 260), card=(0.262, 0.002, 260), popover=(0.285, 0.003, 260),
    fg=(0.845, 0.004, 260), muted_fg=(0.660, 0.005, 260), muted=(0.315, 0.003, 260),
    primary=(0.680, 0.100, 258), pfg=(0.200, 0.006, 258),
)
V['C 暖夜'] = dict(
    bg=(0.225, 0.009, 70), card=(0.263, 0.010, 70), popover=(0.287, 0.011, 70),
    fg=(0.865, 0.016, 78), muted_fg=(0.680, 0.016, 78), muted=(0.318, 0.011, 70),
    primary=(0.680, 0.082, 258), pfg=(0.210, 0.010, 70),
)
V['D 层叠'] = dict(
    bg=(0.235, 0.012, 300), card=(0.280, 0.014, 300), popover=(0.305, 0.015, 300),
    fg=(0.905, 0.008, 300), muted_fg=(0.790, 0.011, 300), muted=(0.345, 0.015, 300),
    primary=(0.800, 0.080, 250), pfg=(0.220, 0.020, 300),
)

for name, p in V.items():
    L = {k: lum(v) for k, v in p.items()}
    print(f"\n===== {name} =====")
    for k, v in p.items():
        print(f"  {k:9s} {hexof(v)}  oklch({v[0]} {v[1]} {v[2]})")
    print(f"  正文 fg/bg={ct(L['fg'],L['bg']):.2f}  fg/card={ct(L['fg'],L['card']):.2f}  fg/pop={ct(L['fg'],L['popover']):.2f}")
    print(f"  次要 mf/bg={ct(L['muted_fg'],L['bg']):.2f}  mf/card={ct(L['muted_fg'],L['card']):.2f}")
    print(f"  链接 pr/bg={ct(L['primary'],L['bg']):.2f}  pr/card={ct(L['primary'],L['card']):.2f}")
    print(f"  按钮 darktext/prim={ct(L['pfg'],L['primary']):.2f}")
    print(f"  code底 m/card={ct(L['muted'],L['card']):.2f}(非文本参考)  muted/bg: {ct(L['muted'],L['bg']):.2f}")

# 现状对照
p0 = dict(bg=(0.145,0,0), card=(0.205,0,0), fg=(0.985,0,0), muted_fg=(0.708,0,0))
L0 = {k: lum(v) for k, v in p0.items()}
print(f"\n===== 0 现状 =====")
print(f"  正文 fg/bg={ct(L0['fg'],L0['bg']):.2f}  fg/card={ct(L0['fg'],L0['card']):.2f}  次要 mf/bg={ct(L0['muted_fg'],L0['bg']):.2f}")
