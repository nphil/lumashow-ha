# LumaShow brand images

Home Assistant core serves `custom_components/<domain>/brand/<image>.png` directly, so these files show up in the UI and in HACS with no submission to the `home-assistant/brands` repository.

| File | Size |
| --- | --- |
| `icon.png` / `icon@2x.png` | 256x256 / 512x512 |
| `logo.png` / `logo@2x.png` | 864x256 / 1728x512 |
| `dark_icon.png` / `dark_icon@2x.png` | 256x256 / 512x512 |
| `dark_logo.png` / `dark_logo@2x.png` | 864x256 / 1728x512 |

The mark is the LumaShow app icon: a glass "light path" L with a warm core, on a dark rounded tile (`icon.svg` at the repository root is the vector original). The logos put it beside the LumaShow wordmark on a transparent background.

All eight PNGs are rendered by `tools/render_brand.py` (Pillow only, deterministic). Do not edit the PNGs by hand: change the script and run it again.

`dark_*` variants use a pale tile with a deeper violet mark, and a light wordmark, so they keep their contrast on Home Assistant's dark header; the others are the app icon as it is.
