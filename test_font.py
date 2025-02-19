import matplotlib
import matplotlib.font_manager as fm

# 1) Path to your .ttc
ttc_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# 2) Force Matplotlib to register that font
fm.fontManager.addfont(ttc_path)

# 3) Check what Matplotlib sees as the "family" name
prop = fm.FontProperties(fname=ttc_path)
actual_name = prop.get_name()

print("Matplotlib version:", matplotlib.__version__)
print("Matplotlib sees this font’s internal name as:", actual_name)
